from enum import Enum
import struct
from typing import Any, List

class ModbusDataType(Enum):
    UINT16 = "Unsigned Integer"
    INT16 = "Signed Integer"
    UINT32 = "Unsigned Double Word"
    INT32 = "Signed Double Word"
    FLOAT32 = "Floating Point"
    SWAPPED_FLOAT32 = "Swapped Floating Point"

class ByteOrder(Enum):
    BIG_ENDIAN = ">"
    LITTLE_ENDIAN = "<"

class WordOrder(Enum):
    ABCD = "ABCD"
    CDAB = "CDAB" # Word swapped

class DataConverter:
    @staticmethod
    def to_registers(value: Any, data_type: ModbusDataType, byte_order: ByteOrder = ByteOrder.BIG_ENDIAN, word_order: WordOrder = WordOrder.ABCD) -> List[int]:
        """Converts a value to a list of 16-bit register values."""
        try:
            val = float(value)
            
            # Handle 16-bit types
            if data_type == ModbusDataType.UINT16:
                return [int(val) & 0xFFFF]
            
            elif data_type == ModbusDataType.INT16:
                # Pack as signed 16-bit and unpack as unsigned 16-bit
                packed = struct.pack(f"{byte_order.value}h", int(val))
                return list(struct.unpack(">H", packed))
            
            # Handle 32-bit types
            elif data_type in [ModbusDataType.UINT32, ModbusDataType.INT32, ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32]:
                fmt = f"{byte_order.value}"
                if data_type == ModbusDataType.FLOAT32: fmt += "f"
                elif data_type == ModbusDataType.SWAPPED_FLOAT32: fmt = "<f" # Always little-endian for swapped
                elif data_type == ModbusDataType.UINT32: fmt += "I"
                elif data_type == ModbusDataType.INT32: fmt += "i"
                
                # If swapping is required, pack accordingly
                if data_type == ModbusDataType.SWAPPED_FLOAT32:
                    packed = struct.pack(fmt, val)
                else:
                    packed = struct.pack(fmt, val)
                
                # Unpack into two 16-bit registers (Big-Endian is standard for Modbus)
                regs = list(struct.unpack(">HH", packed))
                
                if word_order == WordOrder.CDAB:
                    regs[0], regs[1] = regs[1], regs[0]
                    
                return regs
        except Exception:
            return [0]
        return [0]

    @staticmethod
    def from_registers(registers: List[int], data_type: ModbusDataType) -> Any:
        """Converts a list of 16-bit registers back to a value."""
        return 0

class Register:
    def __init__(self, address: int, data_type: ModbusDataType = ModbusDataType.UINT16):
        self.address = address
        self.data_type = data_type
        self.value = 0

    def to_dict(self):
        return {
            "address": self.address,
            "data_type": self.data_type.value,
        }
