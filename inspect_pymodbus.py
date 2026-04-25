from pymodbus.datastore import ModbusDeviceContext
import inspect

print("Checking ModbusDeviceContext constructor...")
print(inspect.signature(ModbusDeviceContext))
