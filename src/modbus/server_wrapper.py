"""
server_wrapper.py – Modbus TCP server thread.

One ModbusServer instance = one TCP endpoint (IP:port).
It can host multiple slave IDs, each with independent register maps.
"""

import threading
import logging
import asyncio
from pymodbus.server import StartAsyncTcpServer
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Per-slave data-block container ─────────────────────────────────────────────
class SlaveDataBlocks:
    """Holds the four pymodbus data blocks for one slave ID."""

    # Full Modbus range: 65536 registers (0x0000–0xFFFF).
    # We allocate 65537 slots so that 1-based addressing (addr 1–65536)
    # fits without remapping (slot 0 is unused in 1-based mode).
    _BLOCK_SIZE = 65537

    def __init__(self):
        self.coils             = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.discrete_inputs   = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.holding_registers = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)
        self.input_registers   = ModbusSequentialDataBlock(0, [0] * self._BLOCK_SIZE)

    def get_store(self, reg_type: str):
        return {
            "coils":             self.coils,
            "discrete_inputs":   self.discrete_inputs,
            "holding_registers": self.holding_registers,
            "input_registers":   self.input_registers,
        }.get(reg_type)

    def to_slave_context(self) -> ModbusSlaveContext:
        return ModbusSlaveContext(
            co=self.coils,
            di=self.discrete_inputs,
            hr=self.holding_registers,
            ir=self.input_registers,
        )


# ══════════════════════════════════════════════════════════════════════════════
class ModbusServer(threading.Thread):
    """
    A Modbus TCP server running in its own thread.

    Parameters
    ----------
    host : str
        IP address to bind to.
    port : int
        TCP port (default 502).
    zero_based : bool
        If True, register addresses start at 0; otherwise at 1.
    slave_ids : list[int]
        Slave IDs to pre-create.  Additional slaves can be added later
        (before the server starts) via add_slave().
    """

    def __init__(self,
                 host: str = "0.0.0.0",
                 port: int = 502,
                 zero_based: bool = False,
                 slave_ids: list | None = None):
        super().__init__(daemon=True)
        self.host       = host
        self.port       = port
        self.zero_based = zero_based
        self.running    = False

        # slave_id → SlaveDataBlocks
        self._slaves: dict[int, SlaveDataBlocks] = {}
        for sid in (slave_ids or []):
            self._slaves[sid] = SlaveDataBlocks()

        self._context: ModbusServerContext | None = None
        self._loop   = asyncio.new_event_loop()
        self._server = None

    # ──────────────────────────────────────────────────────────────────────────
    # Slave management (call before start())
    # ──────────────────────────────────────────────────────────────────────────

    def add_slave(self, slave_id: int) -> SlaveDataBlocks:
        """Add a new slave (must be called before start())."""
        if slave_id in self._slaves:
            return self._slaves[slave_id]
        blocks = SlaveDataBlocks()
        self._slaves[slave_id] = blocks
        return blocks

    def get_slave_blocks(self, slave_id: int) -> SlaveDataBlocks | None:
        return self._slaves.get(slave_id)

    # ──────────────────────────────────────────────────────────────────────────
    # Address helper
    # ──────────────────────────────────────────────────────────────────────────

    def _row_to_address(self, row: int) -> int:
        return row if self.zero_based else row + 1

    # ──────────────────────────────────────────────────────────────────────────
    # Public data-write API
    # ──────────────────────────────────────────────────────────────────────────

    def set_initial_values(self, slave_id: int, reg_type: str, values: list):
        """
        Bulk-write raw 16-bit (or boolean) values for one slave's register group.
        `values` length must match the number of configured rows (up to 65536).
        """
        blocks = self._slaves.get(slave_id)
        if blocks is None:
            logger.warning(f"set_initial_values: unknown slave {slave_id}")
            return
        store = blocks.get_store(reg_type)
        if store is None:
            return
        start_addr = 0 if self.zero_based else 1
        store.setValues(start_addr, values)
        logger.info(f"[slave {slave_id}] {reg_type}: bulk-wrote {len(values)} values")

    def update_register(self, slave_id: int, reg_type: str, row: int, value: int):
        """Write a single 16-bit (or boolean) value."""
        blocks = self._slaves.get(slave_id)
        if blocks is None:
            return
        store = blocks.get_store(reg_type)
        if store is None:
            return
        try:
            addr = self._row_to_address(row)
            store.setValues(addr, [value])
        except Exception as e:
            logger.error(f"update_register slave={slave_id} {reg_type} row={row}: {e}")

    def update_registers(self, slave_id: int, reg_type: str,
                         start_row: int, values: list):
        """Write multiple consecutive 16-bit values (for 32/64-bit types)."""
        blocks = self._slaves.get(slave_id)
        if blocks is None:
            return
        store = blocks.get_store(reg_type)
        if store is None:
            return
        try:
            addr = self._row_to_address(start_row)
            store.setValues(addr, values)
        except Exception as e:
            logger.error(
                f"update_registers slave={slave_id} {reg_type} "
                f"start_row={start_row}: {e}"
            )

    def set_zero_based(self, zero_based: bool):
        """Switch addressing mode at runtime (remaps all slaves)."""
        if self.zero_based == zero_based:
            return
        logger.info(f"Switching zero_based: {self.zero_based} → {zero_based}")

        def remap(store):
            old_start = 0 if self.zero_based else 1
            values = store.getValues(old_start, 65536)
            store.setValues(0, [0] * SlaveDataBlocks._BLOCK_SIZE)
            new_start = 0 if zero_based else 1
            store.setValues(new_start, values)

        for blocks in self._slaves.values():
            remap(blocks.coils)
            remap(blocks.discrete_inputs)
            remap(blocks.holding_registers)
            remap(blocks.input_registers)

        self.zero_based = zero_based

    # ──────────────────────────────────────────────────────────────────────────
    # Thread lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        logger.info(f"Starting Modbus server on {self.host}:{self.port} "
                    f"with slaves {list(self._slaves.keys())}")

        identity = ModbusDeviceIdentification()
        identity.VendorName  = "Simulator"
        identity.ProductCode = "MS"
        identity.ModelName   = "Modbus Server Simulator"

        # Build the server context from all registered slaves
        self._context = ModbusServerContext(
            slaves={sid: blocks.to_slave_context()
                    for sid, blocks in self._slaves.items()},
            single=False,
        )

        self.running = True
        asyncio.set_event_loop(self._loop)

        async def run_server():
            self._server = await StartAsyncTcpServer(
                context=self._context,
                identity=identity,
                address=(self.host, self.port),
            )
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(run_server())
        except Exception as e:
            logger.error(f"Server {self.host}:{self.port} error: {e}")
        finally:
            self.running = False
            logger.info(f"Modbus server {self.host}:{self.port} stopped.")

    def stop(self):
        logger.info(f"Stop requested for {self.host}:{self.port}")
        self.running = False

        if self._server:
            async def _shutdown():
                try:
                    await self._server.shutdown()
                except Exception as e:
                    logger.error(f"Shutdown error: {e}")
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)

        self._loop.call_soon_threadsafe(self._loop.stop)
