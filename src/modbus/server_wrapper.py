"""
server_wrapper.py – Modbus TCP server thread and supporting data-block types.

Provides
--------
DatastoreProtocol       – typing.Protocol defining the interface for a data block.
SlaveDataBlocks         – Concrete data block container for one slave ID.
ConflictingDataBlock    – Data block that XORs two slaves' data to simulate
                          RS-485 bus contention (duplicate address conflict).
ModbusServerFactory     – Factory that builds a ModbusServer from a ServerConfig,
                          automatically wiring ConflictingDataBlocks where needed.
ModbusServer            – Thread that runs a pymodbus async TCP server.

RS-485 Conflict Simulation
---------------------------
When two physical Modbus devices share the same slave address on an RS-485 bus
and both respond to a query simultaneously, their differential drivers contend
for the bus.  Bits where the two devices agree pass through cleanly; bits where
they disagree collapse to near-zero voltage, which the receiver interprets
randomly.  The net effect is that the master receives a byte-stream that is the
bitwise XOR of both devices' payloads, with ~15 % of frames dropped entirely
(too corrupted to parse).

We model this here by:
  1. XOR-ing the register values from both SlaveDataBlocks on every read.
  2. Randomly returning an empty list ~15 % of the time (simulates a dropped
     frame / timeout from the master's perspective).
"""

from __future__ import annotations

import random
import threading
import logging
import asyncio
from typing import Protocol, runtime_checkable

from pymodbus.server import StartAsyncTcpServer
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)

logger = logging.getLogger(__name__)

# Probability that a conflicting read returns nothing (simulates a frame so
# corrupted the master sees a timeout / no response).
_CONFLICT_DROP_PROBABILITY = 0.15


# ══════════════════════════════════════════════════════════════════════════════
# Protocol (interface) for data blocks — enables Dependency Inversion
# ══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class DatastoreProtocol(Protocol):
    """
    Minimal interface that any data block must satisfy.

    Both ``ModbusSequentialDataBlock`` and ``ConflictingDataBlock`` implement
    this protocol, so ``ModbusServer`` depends on the abstraction rather than
    the concrete pymodbus class.
    """

    def getValues(self, address: int, count: int = 1) -> list:  # noqa: N802
        """Return *count* values starting at *address*."""
        ...

    def setValues(self, address: int, values: list) -> None:  # noqa: N802
        """Write *values* starting at *address*."""
        ...

    def validate(self, address: int, count: int = 1) -> bool:
        """Return True if the address range is valid."""
        ...


# ══════════════════════════════════════════════════════════════════════════════
# Per-slave data-block container
# ══════════════════════════════════════════════════════════════════════════════

class SlaveDataBlocks:
    """
    Holds the four pymodbus data blocks for one slave ID.

    Block size is 65 537 slots so that 1-based addressing (addr 1–65 536)
    fits without remapping (slot 0 is unused in 1-based mode).
    """

    _BLOCK_SIZE = 65_537

    def __init__(self):
        self.coils             = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.discrete_inputs   = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.holding_registers = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.input_registers   = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)

    def get_store(self, reg_type: str) -> ModbusSequentialDataBlock | None:
        """Return the data block for *reg_type*, or None if unknown."""
        return {
            "coils":             self.coils,
            "discrete_inputs":   self.discrete_inputs,
            "holding_registers": self.holding_registers,
            "input_registers":   self.input_registers,
        }.get(reg_type)

    def to_slave_context(self) -> ModbusSlaveContext:
        """Wrap this block set in a pymodbus ModbusSlaveContext."""
        return ModbusSlaveContext(
            co=self.coils,
            di=self.discrete_inputs,
            hr=self.holding_registers,
            ir=self.input_registers,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Conflicting data block — simulates RS-485 bus contention
# ══════════════════════════════════════════════════════════════════════════════

class ConflictingDataBlock:
    """
    A data block that simulates RS-485 bus contention between two devices
    sharing the same Modbus slave address.

    On every ``getValues`` call:
      • With probability ``_CONFLICT_DROP_PROBABILITY`` the call returns an
        empty list, simulating a frame too corrupted to parse (master timeout).
      • Otherwise it returns the element-wise XOR of both blocks' values,
        simulating the superposition of two simultaneous RS-485 transmissions.

    ``setValues`` writes to *both* underlying blocks so that each device's
    register map stays independently editable in the GUI.

    Parameters
    ----------
    block_a : ModbusSequentialDataBlock
        Data block for the first conflicting device.
    block_b : ModbusSequentialDataBlock
        Data block for the second conflicting device.
    drop_probability : float
        Fraction of reads that return nothing (0.0–1.0).
    """

    def __init__(
        self,
        block_a: ModbusSequentialDataBlock,
        block_b: ModbusSequentialDataBlock,
        drop_probability: float = _CONFLICT_DROP_PROBABILITY,
    ):
        self._block_a         = block_a
        self._block_b         = block_b
        self._drop_probability = drop_probability

    # ── DatastoreProtocol implementation ──────────────────────────────────────

    def getValues(self, address: int, count: int = 1) -> list:  # noqa: N802
        """
        Return XOR'd values from both blocks, or an empty list (dropped frame).
        """
        if random.random() < self._drop_probability:
            logger.debug(
                "ConflictingDataBlock: dropping frame at addr=%d count=%d "
                "(simulated RS-485 collision dropout)", address, count
            )
            return []

        vals_a = self._block_a.getValues(address, count)
        vals_b = self._block_b.getValues(address, count)

        # Pad to equal length (should always be equal, but be defensive)
        length = max(len(vals_a), len(vals_b))
        vals_a = list(vals_a) + [0] * (length - len(vals_a))
        vals_b = list(vals_b) + [0] * (length - len(vals_b))

        corrupted = [a ^ b for a, b in zip(vals_a, vals_b)]
        logger.debug(
            "ConflictingDataBlock: addr=%d count=%d → XOR result %s",
            address, count, corrupted,
        )
        return corrupted

    def setValues(self, address: int, values: list) -> None:  # noqa: N802
        """Write to block_a only (block_b is managed independently via the GUI)."""
        self._block_a.setValues(address, values)

    def validate(self, address: int, count: int = 1) -> bool:
        """Delegate validation to block_a."""
        return self._block_a.validate(address, count)

    # ── Direct write access for the GUI ───────────────────────────────────────

    def set_values_a(self, address: int, values: list) -> None:
        """Write to the first device's block."""
        self._block_a.setValues(address, values)

    def set_values_b(self, address: int, values: list) -> None:
        """Write to the second device's block."""
        self._block_b.setValues(address, values)


# ══════════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════════

class ModbusServerFactory:
    """
    Creates a ``ModbusServer`` from a ``ServerConfig``.

    Automatically detects slave IDs that appear more than once and wires
    ``ConflictingDataBlock`` instances for those IDs so that the running
    server faithfully simulates RS-485 address conflicts.
    """

    @staticmethod
    def create(srv_config) -> "ModbusServer":
        """
        Build and return a configured (but not yet started) ``ModbusServer``.

        Parameters
        ----------
        srv_config : ServerConfig
            The server configuration to build from.

        Returns
        -------
        ModbusServer
            Ready to call ``.start()`` on.
        """
        from models.device_config import ServerConfig  # avoid circular at module level
        assert isinstance(srv_config, ServerConfig)

        ms = ModbusServer(
            host       = srv_config.host,
            port       = srv_config.port,
            zero_based = srv_config.zero_based,
        )

        # Group slaves by ID so we can detect conflicts
        slaves_by_id: dict[int, list] = {}
        for slave in srv_config.slaves:
            slaves_by_id.setdefault(slave.slave_id, []).append(slave)

        for slave_id, slave_list in slaves_by_id.items():
            if len(slave_list) == 1:
                # Normal slave — one set of data blocks
                ms.add_slave(slave_id)
            else:
                # Conflicting slaves — create a ConflictingDataBlock per pair
                # (we support exactly 2 conflicting devices per ID, which is
                # the realistic field scenario)
                ms.add_slave(slave_id)
                ms.mark_as_conflicting(slave_id, len(slave_list))
                logger.info(
                    "ModbusServerFactory: slave ID %d has %d conflicting devices "
                    "on %s:%d — ConflictingDataBlock will be used.",
                    slave_id, len(slave_list), srv_config.host, srv_config.port,
                )

        return ms


# ══════════════════════════════════════════════════════════════════════════════
# Modbus TCP server thread
# ══════════════════════════════════════════════════════════════════════════════

class ModbusServer(threading.Thread):
    """
    A Modbus TCP server running in its own daemon thread.

    One ``ModbusServer`` instance = one TCP endpoint (IP:port).
    It can host multiple slave IDs, each with independent register maps.
    Slave IDs that are marked as conflicting use ``ConflictingDataBlock``
    to simulate RS-485 bus contention.

    Parameters
    ----------
    host : str
        IP address to bind to.
    port : int
        TCP port (default 502).
    zero_based : bool
        If True, register addresses start at 0; otherwise at 1.
    slave_ids : list[int] | None
        Slave IDs to pre-create.  Additional slaves can be added later
        (before the server starts) via ``add_slave()``.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 502,
        zero_based: bool = False,
        slave_ids: list | None = None,
    ):
        super().__init__(daemon=True)
        self.host       = host
        self.port       = port
        self.zero_based = zero_based
        self.running    = False

        # slave_id → SlaveDataBlocks (primary blocks)
        self._slaves: dict[int, SlaveDataBlocks] = {}
        # slave_id → list[SlaveDataBlocks] for conflict simulation (secondary blocks)
        self._conflict_extra: dict[int, list[SlaveDataBlocks]] = {}

        for sid in (slave_ids or []):
            self._slaves[sid] = SlaveDataBlocks()

        self._context: ModbusServerContext | None = None
        self._loop    = asyncio.new_event_loop()
        self._server  = None

    # ──────────────────────────────────────────────────────────────────────────
    # Slave management (call before start())
    # ──────────────────────────────────────────────────────────────────────────

    def add_slave(self, slave_id: int) -> SlaveDataBlocks:
        """
        Add a new slave (must be called before ``start()``).

        If the slave already exists, the existing blocks are returned.
        """
        if slave_id not in self._slaves:
            self._slaves[slave_id] = SlaveDataBlocks()
        return self._slaves[slave_id]

    def mark_as_conflicting(self, slave_id: int, device_count: int = 2):
        """
        Mark *slave_id* as having *device_count* conflicting devices.

        Creates additional ``SlaveDataBlocks`` for the extra devices.
        Must be called before ``start()``.
        """
        extras = []
        for _ in range(device_count - 1):   # primary block already exists
            extras.append(SlaveDataBlocks())
        self._conflict_extra[slave_id] = extras

    def get_slave_blocks(self, slave_id: int) -> SlaveDataBlocks | None:
        """Return the primary data blocks for *slave_id*, or None."""
        return self._slaves.get(slave_id)

    def get_conflict_blocks(self, slave_id: int) -> list[SlaveDataBlocks]:
        """Return the list of extra (conflicting) data blocks for *slave_id*."""
        return self._conflict_extra.get(slave_id, [])

    def is_conflicting(self, slave_id: int) -> bool:
        """Return True if *slave_id* has conflicting devices."""
        return slave_id in self._conflict_extra

    # ──────────────────────────────────────────────────────────────────────────
    # Address helper
    # ──────────────────────────────────────────────────────────────────────────

    def _row_to_address(self, row: int) -> int:
        """Convert a 0-based row index to a Modbus register address."""
        return row if self.zero_based else row + 1

    # ──────────────────────────────────────────────────────────────────────────
    # Public data-write API
    # ──────────────────────────────────────────────────────────────────────────

    def update_register(self, slave_id: int, reg_type: str,
                        row: int, value: int):
        """
        Write a single 16-bit (or boolean) value to the primary data block.

        For conflicting slaves, this updates the *primary* device's block only.
        Use ``update_register_conflict(slave_id, device_index, ...)`` to update
        a specific conflicting device.
        """
        blocks = self._slaves.get(slave_id)
        if blocks is None:
            return
        store = blocks.get_store(reg_type)
        if store is None:
            return
        try:
            store.setValues(self._row_to_address(row), [value])
        except Exception as exc:
            logger.error(
                "update_register slave=%d %s row=%d: %s", slave_id, reg_type, row, exc
            )

    def update_registers(self, slave_id: int, reg_type: str,
                         start_row: int, values: list):
        """
        Write multiple consecutive 16-bit values to the primary data block.

        For conflicting slaves, this updates the *primary* device's block only.
        """
        blocks = self._slaves.get(slave_id)
        if blocks is None:
            return
        store = blocks.get_store(reg_type)
        if store is None:
            return
        try:
            store.setValues(self._row_to_address(start_row), values)
        except Exception as exc:
            logger.error(
                "update_registers slave=%d %s start_row=%d: %s",
                slave_id, reg_type, start_row, exc,
            )

    def update_register_conflict(self, slave_id: int, device_index: int,
                                 reg_type: str, row: int, value: int):
        """
        Write a single value to a *specific* conflicting device's block.

        Parameters
        ----------
        slave_id : int
            The shared slave ID.
        device_index : int
            0 = primary device, 1 = first extra device, etc.
        reg_type : str
            Register type key (e.g. "holding_registers").
        row : int
            0-based row index.
        value : int
            16-bit value to write.
        """
        if device_index == 0:
            self.update_register(slave_id, reg_type, row, value)
            return
        extras = self._conflict_extra.get(slave_id, [])
        idx = device_index - 1
        if idx >= len(extras):
            logger.warning(
                "update_register_conflict: device_index=%d out of range for slave %d",
                device_index, slave_id,
            )
            return
        store = extras[idx].get_store(reg_type)
        if store is None:
            return
        try:
            store.setValues(self._row_to_address(row), [value])
        except Exception as exc:
            logger.error(
                "update_register_conflict slave=%d device=%d %s row=%d: %s",
                slave_id, device_index, reg_type, row, exc,
            )

    def update_registers_conflict(self, slave_id: int, device_index: int,
                                  reg_type: str, start_row: int, values: list):
        """
        Write multiple values to a *specific* conflicting device's block.
        """
        if device_index == 0:
            self.update_registers(slave_id, reg_type, start_row, values)
            return
        extras = self._conflict_extra.get(slave_id, [])
        idx = device_index - 1
        if idx >= len(extras):
            return
        store = extras[idx].get_store(reg_type)
        if store is None:
            return
        try:
            store.setValues(self._row_to_address(start_row), values)
        except Exception as exc:
            logger.error(
                "update_registers_conflict slave=%d device=%d %s start_row=%d: %s",
                slave_id, device_index, reg_type, start_row, exc,
            )

    def set_zero_based(self, zero_based: bool):
        """Switch addressing mode at runtime (remaps all slaves)."""
        if self.zero_based == zero_based:
            return
        logger.info("Switching zero_based: %s → %s", self.zero_based, zero_based)

        def remap(store: ModbusSequentialDataBlock):
            old_start = 0 if self.zero_based else 1
            values = store.getValues(old_start, 65_536)
            store.setValues(0, [0] * SlaveDataBlocks._BLOCK_SIZE)
            new_start = 0 if zero_based else 1
            store.setValues(new_start, values)

        all_block_sets = list(self._slaves.values())
        for extras in self._conflict_extra.values():
            all_block_sets.extend(extras)

        for blocks in all_block_sets:
            remap(blocks.coils)
            remap(blocks.discrete_inputs)
            remap(blocks.holding_registers)
            remap(blocks.input_registers)

        self.zero_based = zero_based

    # ──────────────────────────────────────────────────────────────────────────
    # Thread lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def _build_slave_context(self, slave_id: int) -> ModbusSlaveContext:
        """
        Build the ModbusSlaveContext for *slave_id*.

        For conflicting slaves, wraps each register type in a
        ConflictingDataBlock that XORs the primary and first extra block.
        """
        primary = self._slaves[slave_id]
        extras  = self._conflict_extra.get(slave_id, [])

        if not extras:
            return primary.to_slave_context()

        # Use the first extra block for the conflict (two-device scenario)
        extra = extras[0]
        return ModbusSlaveContext(
            co=ConflictingDataBlock(primary.coils,             extra.coils),
            di=ConflictingDataBlock(primary.discrete_inputs,   extra.discrete_inputs),
            hr=ConflictingDataBlock(primary.holding_registers, extra.holding_registers),
            ir=ConflictingDataBlock(primary.input_registers,   extra.input_registers),
        )

    def run(self):
        """Thread entry point — starts the async Modbus TCP server."""
        logger.info(
            "Starting Modbus server on %s:%d with slaves %s",
            self.host, self.port, list(self._slaves.keys()),
        )

        identity = ModbusDeviceIdentification()
        identity.VendorName  = "Simulator"
        identity.ProductCode = "MS"
        identity.ModelName   = "Modbus Server Simulator"

        self._context = ModbusServerContext(
            slaves={sid: self._build_slave_context(sid)
                    for sid in self._slaves},
            single=False,
        )

        self.running = True
        asyncio.set_event_loop(self._loop)

        async def _run_server():
            self._server = await StartAsyncTcpServer(
                context  = self._context,
                identity = identity,
                address  = (self.host, self.port),
            )
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(_run_server())
        except Exception as exc:
            logger.error("Server %s:%d error: %s", self.host, self.port, exc)
        finally:
            self.running = False
            logger.info("Modbus server %s:%d stopped.", self.host, self.port)

    def stop(self):
        """Request the server to stop."""
        logger.info("Stop requested for %s:%d", self.host, self.port)
        self.running = False

        if self._server:
            async def _shutdown():
                try:
                    await self._server.shutdown()
                except Exception as exc:
                    logger.error("Shutdown error: %s", exc)

            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)

        self._loop.call_soon_threadsafe(self._loop.stop)
