try:
    from pymodbus.datastore import ModbusSlaveContext
    print("Found in pymodbus.datastore")
except ImportError:
    try:
        from pymodbus.server import ModbusSlaveContext
        print("Found in pymodbus.server")
    except ImportError:
        try:
            import pymodbus
            print(f"Pymodbus version: {pymodbus.__version__}")
            # Try to find it
            import importlib
            for name in ["pymodbus.datastore", "pymodbus.server", "pymodbus.device"]:
                try:
                    mod = importlib.import_module(name)
                    if hasattr(mod, 'ModbusSlaveContext'):
                        print(f"Found in {name}")
                except:
                    pass
        except:
            print("Could not find pymodbus")
