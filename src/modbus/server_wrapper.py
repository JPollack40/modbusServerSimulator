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

class ModbusServer(threading.Thread):
    def __init__(self, slave_id=1, host="0.0.0.0", port=502, zero_based=False):
        super().__init__()
        self.slave_id   = slave_id
        self.host       = host
        self.port       = port
        self.zero_based = zero_based
        self.running    = False
        self.daemon     = True  # exits when GUI exits

        # Use 101 slots (addresses 0-100) so both zero-based (0-99) and
        # one-based (1-100) addressing modes are covered without remapping.
        self.coils             = ModbusSequentialDataBlock(0, [0] * 101)
        self.discrete_inputs   = ModbusSequentialDataBlock(0, [0] * 101)
        self.holding_registers = ModbusSequentialDataBlock(0, [0] * 101)
        self.input_registers   = ModbusSequentialDataBlock(0, [0] * 101)

        self.context = ModbusServerContext(
            slaves={self.slave_id: ModbusSlaveContext(
                co=self.coils,
                di=self.discrete_inputs,
                hr=self.holding_registers,
                ir=self.input_registers,
            )},
            single=False,
        )

        self._loop   = asyncio.new_event_loop()
        self._server = None

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_store(self, reg_type: str):
        """Return the data-block for the given register type string."""
        return {
            'coils':             self.coils,
            'discrete_inputs':   self.discrete_inputs,
            'holding_registers': self.holding_registers,
            'input_registers':   self.input_registers,
        }.get(reg_type)

    def _row_to_address(self, row: int) -> int:
        """Convert a 0-based GUI row index to the internal Modbus address."""
        return row if self.zero_based else row + 1

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def set_initial_values(self, reg_type: str, values: list):
        """
        Write a list of raw 16-bit (or boolean) values starting at the
        correct base address for the current addressing mode.
        `values` must have exactly 100 elements (one per GUI row).
        """
        logger.info(f"Setting initial values for {reg_type}")
        store = self._get_store(reg_type)
        if store is None:
            return
        start_addr = 0 if self.zero_based else 1
        store.setValues(start_addr, values)

    def update_register(self, reg_type: str, row: int, value: int):
        """
        Write a single 16-bit (or boolean) value to the register
        corresponding to GUI row `row`.
        """
        store = self._get_store(reg_type)
        if store is None:
            return
        try:
            addr = self._row_to_address(row)
            store.setValues(addr, [value])
            logger.info(f"[{reg_type}] row {row} → addr {addr} = {value}")
        except Exception as e:
            logger.error(f"update_register failed for {reg_type} row {row}: {e}")

    def update_registers(self, reg_type: str, start_row: int, values: list):
        """
        Write multiple consecutive 16-bit values beginning at the register
        corresponding to GUI row `start_row`.  Used for 32-bit and 64-bit
        data types that span 2 or 4 registers respectively.
        """
        store = self._get_store(reg_type)
        if store is None:
            return
        try:
            addr = self._row_to_address(start_row)
            store.setValues(addr, values)
            logger.info(
                f"[{reg_type}] rows {start_row}–{start_row + len(values) - 1} "
                f"→ addrs {addr}–{addr + len(values) - 1} = {values}"
            )
        except Exception as e:
            logger.error(
                f"update_registers failed for {reg_type} "
                f"start_row={start_row}: {e}"
            )

    def set_zero_based(self, zero_based: bool):
        """Switch between zero-based and one-based addressing at runtime."""
        if self.zero_based == zero_based:
            return

        logger.info(f"Switching zero_based: {self.zero_based} → {zero_based}")

        def remap(store):
            old_start = 0 if self.zero_based else 1
            values = store.getValues(old_start, 100)
            store.setValues(0, [0] * 101)          # clear everything
            new_start = 0 if zero_based else 1
            store.setValues(new_start, values)

        remap(self.coils)
        remap(self.discrete_inputs)
        remap(self.holding_registers)
        remap(self.input_registers)

        self.zero_based = zero_based

    # ──────────────────────────────────────────────────────────────────────────
    # Thread lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        logger.info(f"Initializing Modbus server on {self.host}:{self.port}…")

        identity = ModbusDeviceIdentification()
        identity.VendorName  = 'Simulator'
        identity.ProductCode = 'MS'
        identity.ModelName   = 'Modbus Server'

        self.running = True
        asyncio.set_event_loop(self._loop)

        async def run_server():
            self._server = await StartAsyncTcpServer(
                context=self.context,
                identity=identity,
                address=(self.host, self.port),
            )
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(run_server())
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            self.running = False
            logger.info("Modbus server stopped.")

    def stop(self):
        logger.info("Modbus server stop requested")
        self.running = False

        if self._server:
            async def shutdown():
                try:
                    await self._server.shutdown()
                except Exception as e:
                    logger.error(f"Error during server shutdown: {e}")

            asyncio.run_coroutine_threadsafe(shutdown(), self._loop)

        self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Modbus server stop signal sent")
