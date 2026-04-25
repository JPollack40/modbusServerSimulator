import threading
import logging
import asyncio
from pymodbus.server import StartAsyncTcpServer
from pymodbus.pdu.device import ModbusDeviceIdentification
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusDeviceContext, ModbusServerContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ModbusServer(threading.Thread):
    def __init__(self, slave_id=1, host="0.0.0.0", port=502, zero_based=False):
        super().__init__()
        self.slave_id = slave_id
        self.host = host
        self.port = port
        self.zero_based = zero_based
        self.running = False
        self.daemon = True # Ensure thread exits when GUI exits
        
        # Initialize holding registers for different types
        # We need a range that accommodates 0-indexed or 1-indexed.
        # If 0-indexed, we want 0-99.
        # If 1-indexed, we want 1-100.
        # Let's create a range that covers both, e.g., 0-100.
        # ModbusSequentialDataBlock(address, values)
        # For zero_based: address 0, values 100 -> 0-99
        # For one_based: address 1, values 100 -> 1-100
        # Actually, if we use 0-100, we cover both 0-99 and 1-100.
        # Let's use address 0 and 101 values? No, just keep it simple.
        # If we use 0-99, we cover 0-99. If client asks for 100, it's out of range.
        # This is fine for 100 registers.
        
        self.coils = ModbusSequentialDataBlock(0, [0]*101)
        self.discrete_inputs = ModbusSequentialDataBlock(0, [0]*101)
        self.holding_registers = ModbusSequentialDataBlock(0, [0]*101)
        self.input_registers = ModbusSequentialDataBlock(0, [0]*101)

        # In pymodbus 3.x, use ModbusDeviceContext
        self.context = ModbusServerContext(
            devices={self.slave_id: ModbusDeviceContext(
                co=self.coils,
                di=self.discrete_inputs,
                hr=self.holding_registers,
                ir=self.input_registers
            )}, 
            single=False
        )
        
        self._loop = asyncio.new_event_loop()
        self._server = None

    def set_initial_values(self, reg_type, values):
        """Sets initial values for a specific register type."""
        logger.info(f"Setting initial values for {reg_type} at address 0/1: {values}")
        # If zero_based, address 0. If one_based, address 1.
        start_addr = 0 if self.zero_based else 1
        if reg_type == 'coils':
            self.coils.setValues(start_addr, values)
        elif reg_type == 'discrete_inputs':
            self.discrete_inputs.setValues(start_addr, values)
        elif reg_type == 'holding_registers':
            self.holding_registers.setValues(start_addr, values)
        elif reg_type == 'input_registers':
            self.input_registers.setValues(start_addr, values)

    def run(self):
        logger.info(f"Initializing Modbus server on {self.host}:{self.port}...")
        
        identity = ModbusDeviceIdentification()
        identity.VendorName = 'Simulator'
        identity.ProductCode = 'MS'
        identity.ModelName = 'Modbus Server'

        self.running = True
        logger.info(f"Starting Modbus server...")
        
        asyncio.set_event_loop(self._loop)
        
        async def run_server():
            # Start asynchronous server
            self._server = await StartAsyncTcpServer(
                context=self.context,
                identity=identity,
                address=(self.host, self.port)
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
            # Schedule shutdown of the server
            async def shutdown():
                try:
                    await self._server.shutdown()
                except Exception as e:
                    logger.error(f"Error during server shutdown: {e}")
            
            # Use run_coroutine_threadsafe to schedule the shutdown
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
            
        # Stop the loop
        self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Modbus server stop signal sent")

    def set_zero_based(self, zero_based):
        if self.zero_based == zero_based:
            return
        
        logger.info(f"Switching zero_based from {self.zero_based} to {zero_based}")
        
        # Helper to re-map values
        def remap(store):
            # 1. Read current values (100 values)
            start_addr_old = 0 if self.zero_based else 1
            values = store.getValues(start_addr_old, 100)
            
            # 2. Clear all values (0-100)
            store.setValues(0, [0]*101)
            
            # 3. Determine new start address
            new_start = 0 if zero_based else 1
            
            # 4. Write values to new addresses
            store.setValues(new_start, values)
        
        remap(self.coils)
        remap(self.discrete_inputs)
        remap(self.holding_registers)
        remap(self.input_registers)
        
        self.zero_based = zero_based

    def update_register(self, reg_type, address, value):
        logger.info(f"Updating {reg_type} register {address} to {value} for slave {self.slave_id}")
        
        # Choose the correct store
        if reg_type == 'coils':
            store = self.coils
        elif reg_type == 'discrete_inputs':
            store = self.discrete_inputs
        elif reg_type == 'holding_registers':
            store = self.holding_registers
        elif reg_type == 'input_registers':
            store = self.input_registers
        else:
            return
            
        try:
            # Translate request address (row index) to internal index based on addressing mode
            # If zero_based: row 0 -> address 0
            # If one_based: row 0 -> address 1
            
            target_address = address if self.zero_based else address + 1
            
            store.setValues(target_address, [value])
            logger.info(f"Register {target_address} updated successfully (mapped from row {address})")
        except Exception as e:
            logger.error(f"Failed to update register {address}: {e}")
