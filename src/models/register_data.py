from enum import Enum
import struct
from typing import Any, List, Optional

class ModbusDataType(Enum):
    UINT16          = "Unsigned Integer"
    INT16           = "Signed Integer"
    UINT32          = "Unsigned Double Word"
    INT32           = "Signed Double Word"
    FLOAT32         = "Floating Point"
    SWAPPED_FLOAT32 = "Swapped Floating Point"
    UINT64          = "Unsigned 64-bit Integer"
    INT64           = "Signed 64-bit Integer"
    FLOAT64         = "64-bit Float (Double)"
    SWAPPED_FLOAT64 = "Swapped 64-bit Float"

class ByteOrder(Enum):
    BIG_ENDIAN    = ">"
    LITTLE_ENDIAN = "<"

class WordOrder(Enum):
    ABCD = "ABCD"
    CDAB = "CDAB"  # Word swapped

def get_register_count(data_type: 'ModbusDataType') -> int:
    """Returns the number of 16-bit registers required for the given data type."""
    if data_type in (ModbusDataType.UINT16, ModbusDataType.INT16):
        return 1
    elif data_type in (ModbusDataType.UINT32, ModbusDataType.INT32,
                       ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32):
        return 2
    elif data_type in (ModbusDataType.UINT64, ModbusDataType.INT64,
                       ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64):
        return 4
    return 1

class DataConverter:
    @staticmethod
    def to_registers(value: Any, data_type: ModbusDataType,
                     byte_order: ByteOrder = ByteOrder.BIG_ENDIAN,
                     word_order: WordOrder = WordOrder.ABCD) -> List[int]:
        """Converts a value to a list of 16-bit register values."""
        try:
            val = float(value)

            # ── 16-bit types ──────────────────────────────────────────────────
            if data_type == ModbusDataType.UINT16:
                return [int(val) & 0xFFFF]

            elif data_type == ModbusDataType.INT16:
                packed = struct.pack(f"{byte_order.value}h", int(val))
                return list(struct.unpack(">H", packed))

            # ── 32-bit types ──────────────────────────────────────────────────
            elif data_type in (ModbusDataType.UINT32, ModbusDataType.INT32,
                               ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32):
                if data_type == ModbusDataType.FLOAT32:
                    packed = struct.pack(f"{byte_order.value}f", val)
                elif data_type == ModbusDataType.SWAPPED_FLOAT32:
                    # Little-endian byte order, then swap words
                    packed = struct.pack("<f", val)
                elif data_type == ModbusDataType.UINT32:
                    packed = struct.pack(f"{byte_order.value}I", int(val) & 0xFFFFFFFF)
                elif data_type == ModbusDataType.INT32:
                    packed = struct.pack(f"{byte_order.value}i", int(val))

                regs = list(struct.unpack(">HH", packed))

                if data_type == ModbusDataType.SWAPPED_FLOAT32 or word_order == WordOrder.CDAB:
                    regs[0], regs[1] = regs[1], regs[0]

                return regs

            # ── 64-bit types ──────────────────────────────────────────────────
            elif data_type in (ModbusDataType.UINT64, ModbusDataType.INT64,
                               ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64):
                if data_type == ModbusDataType.FLOAT64:
                    packed = struct.pack(f"{byte_order.value}d", val)
                elif data_type == ModbusDataType.SWAPPED_FLOAT64:
                    packed = struct.pack("<d", val)
                elif data_type == ModbusDataType.UINT64:
                    packed = struct.pack(f"{byte_order.value}Q", int(val) & 0xFFFFFFFFFFFFFFFF)
                elif data_type == ModbusDataType.INT64:
                    packed = struct.pack(f"{byte_order.value}q", int(val))

                regs = list(struct.unpack(">HHHH", packed))

                if data_type == ModbusDataType.SWAPPED_FLOAT64 or word_order == WordOrder.CDAB:
                    regs.reverse()

                return regs

        except Exception:
            pass

        return [0] * get_register_count(data_type)

    @staticmethod
    def from_registers(registers: List[int], data_type: ModbusDataType,
                       byte_order: ByteOrder = ByteOrder.BIG_ENDIAN,
                       word_order: WordOrder = WordOrder.ABCD) -> Any:
        """Converts a list of 16-bit registers back to a value."""
        try:
            if data_type == ModbusDataType.UINT16:
                return registers[0]

            elif data_type == ModbusDataType.INT16:
                packed = struct.pack(">H", registers[0])
                return struct.unpack(f"{byte_order.value}h", packed)[0]

            elif data_type in (ModbusDataType.UINT32, ModbusDataType.INT32,
                               ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32):
                regs = list(registers[:2])
                if data_type == ModbusDataType.SWAPPED_FLOAT32 or word_order == WordOrder.CDAB:
                    regs[0], regs[1] = regs[1], regs[0]
                packed = struct.pack(">HH", *regs)
                if data_type == ModbusDataType.FLOAT32:
                    return struct.unpack(f"{byte_order.value}f", packed)[0]
                elif data_type == ModbusDataType.SWAPPED_FLOAT32:
                    return struct.unpack("<f", packed)[0]
                elif data_type == ModbusDataType.UINT32:
                    return struct.unpack(f"{byte_order.value}I", packed)[0]
                elif data_type == ModbusDataType.INT32:
                    return struct.unpack(f"{byte_order.value}i", packed)[0]

            elif data_type in (ModbusDataType.UINT64, ModbusDataType.INT64,
                               ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64):
                regs = list(registers[:4])
                if data_type == ModbusDataType.SWAPPED_FLOAT64 or word_order == WordOrder.CDAB:
                    regs.reverse()
                packed = struct.pack(">HHHH", *regs)
                if data_type == ModbusDataType.FLOAT64:
                    return struct.unpack(f"{byte_order.value}d", packed)[0]
                elif data_type == ModbusDataType.SWAPPED_FLOAT64:
                    return struct.unpack("<d", packed)[0]
                elif data_type == ModbusDataType.UINT64:
                    return struct.unpack(f"{byte_order.value}Q", packed)[0]
                elif data_type == ModbusDataType.INT64:
                    return struct.unpack(f"{byte_order.value}q", packed)[0]

        except Exception:
            pass
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
