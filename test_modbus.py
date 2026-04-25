from pymodbus.datastore import ModbusSequentialDataBlock, ModbusDeviceContext, ModbusServerContext

def test_context():
    try:
        # Initialize holding registers
        store = ModbusDeviceContext(
            hr=ModbusSequentialDataBlock(0, [0]*100)
        )
        
        # Pymodbus 3.x: Initialize context with devices
        context = ModbusServerContext(devices={1: store}, single=False)
        print("Context initialized successfully")
        
        # Test updating
        context[1].setValues(3, 0, [1234])
        print(f"Value set: {context[1].getValues(3, 0, count=1)}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_context()
