"""
register_data.py – Modbus register data types and value conversion utilities.

Provides
--------
ModbusDataType  – Enum of all supported 16/32/64-bit Modbus data types.
ByteOrder       – Big-endian vs little-endian byte ordering.
WordOrder       – ABCD (normal) vs CDAB (word-swapped) word ordering.
get_register_count(dtype) – Number of 16-bit registers required for a type.
DataConverter   – Static methods to encode/decode values to/from register lists.
Register        – Lightweight value-object representing a single Modbus register.
"""

from __future__ import annotations

import struct
from enum import Enum
from typing import Any, List


# ══════════════════════════════════════════════════════════════════════════════
class ModbusDataType(Enum):
    """All data types supported by the simulator's register editor."""

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
    """Byte ordering for multi-byte values."""

    BIG_ENDIAN    = ">"
    LITTLE_ENDIAN = "<"


class WordOrder(Enum):
    """Word ordering for multi-register values."""

    ABCD = "ABCD"   # Normal (high word first)
    CDAB = "CDAB"   # Word-swapped (low word first)


# ── Register-count helper ──────────────────────────────────────────────────────

def get_register_count(data_type: ModbusDataType) -> int:
    """Return the number of 16-bit registers required for *data_type*."""
    if data_type in (ModbusDataType.UINT16, ModbusDataType.INT16):
        return 1
    if data_type in (
        ModbusDataType.UINT32, ModbusDataType.INT32,
        ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32,
    ):
        return 2
    if data_type in (
        ModbusDataType.UINT64, ModbusDataType.INT64,
        ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64,
    ):
        return 4
    return 1


# ══════════════════════════════════════════════════════════════════════════════
class DataConverter:
    """
    Static conversion utilities between Python values and Modbus register lists.

    All methods return sensible defaults (zeros) on conversion failure rather
    than raising, so that a bad user-entered value never crashes the server.
    """

    @staticmethod
    def to_registers(
        value: Any,
        data_type: ModbusDataType,
        byte_order: ByteOrder = ByteOrder.BIG_ENDIAN,
        word_order: WordOrder = WordOrder.ABCD,
    ) -> List[int]:
        """
        Convert *value* to a list of 16-bit unsigned integers suitable for
        writing into a Modbus data block.

        Parameters
        ----------
        value : Any
            The value to encode (will be coerced to float/int as needed).
        data_type : ModbusDataType
            Target encoding.
        byte_order : ByteOrder
            Byte order within each 16-bit word (default: big-endian).
        word_order : WordOrder
            Word order for multi-register types (default: ABCD / high-word first).

        Returns
        -------
        list[int]
            List of 16-bit register values.  Returns all-zeros on failure.
        """
        try:
            val = float(value)

            # ── 16-bit types ──────────────────────────────────────────────────
            if data_type == ModbusDataType.UINT16:
                return [int(val) & 0xFFFF]

            if data_type == ModbusDataType.INT16:
                packed = struct.pack(f"{byte_order.value}h", int(val))
                return list(struct.unpack(">H", packed))

            # ── 32-bit types ──────────────────────────────────────────────────
            if data_type in (
                ModbusDataType.UINT32, ModbusDataType.INT32,
                ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32,
            ):
                if data_type == ModbusDataType.FLOAT32:
                    packed = struct.pack(f"{byte_order.value}f", val)
                elif data_type == ModbusDataType.SWAPPED_FLOAT32:
                    packed = struct.pack("<f", val)
                elif data_type == ModbusDataType.UINT32:
                    packed = struct.pack(f"{byte_order.value}I", int(val) & 0xFFFF_FFFF)
                else:  # INT32
                    packed = struct.pack(f"{byte_order.value}i", int(val))

                regs = list(struct.unpack(">HH", packed))
                if data_type == ModbusDataType.SWAPPED_FLOAT32 or word_order == WordOrder.CDAB:
                    regs[0], regs[1] = regs[1], regs[0]
                return regs

            # ── 64-bit types ──────────────────────────────────────────────────
            if data_type in (
                ModbusDataType.UINT64, ModbusDataType.INT64,
                ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64,
            ):
                if data_type == ModbusDataType.FLOAT64:
                    packed = struct.pack(f"{byte_order.value}d", val)
                elif data_type == ModbusDataType.SWAPPED_FLOAT64:
                    packed = struct.pack("<d", val)
                elif data_type == ModbusDataType.UINT64:
                    packed = struct.pack(
                        f"{byte_order.value}Q", int(val) & 0xFFFF_FFFF_FFFF_FFFF
                    )
                else:  # INT64
                    packed = struct.pack(f"{byte_order.value}q", int(val))

                regs = list(struct.unpack(">HHHH", packed))
                if data_type == ModbusDataType.SWAPPED_FLOAT64 or word_order == WordOrder.CDAB:
                    regs.reverse()
                return regs

        except Exception:
            pass

        return [0] * get_register_count(data_type)

    @staticmethod
    def from_registers(
        registers: List[int],
        data_type: ModbusDataType,
        byte_order: ByteOrder = ByteOrder.BIG_ENDIAN,
        word_order: WordOrder = WordOrder.ABCD,
    ) -> Any:
        """
        Convert a list of 16-bit register values back to a Python value.

        Parameters
        ----------
        registers : list[int]
            Raw 16-bit register values from a Modbus data block.
        data_type : ModbusDataType
            Encoding to use for interpretation.
        byte_order : ByteOrder
            Byte order within each 16-bit word (default: big-endian).
        word_order : WordOrder
            Word order for multi-register types (default: ABCD / high-word first).

        Returns
        -------
        Any
            Decoded value, or 0 on failure.
        """
        try:
            if data_type == ModbusDataType.UINT16:
                return registers[0]

            if data_type == ModbusDataType.INT16:
                packed = struct.pack(">H", registers[0])
                return struct.unpack(f"{byte_order.value}h", packed)[0]

            if data_type in (
                ModbusDataType.UINT32, ModbusDataType.INT32,
                ModbusDataType.FLOAT32, ModbusDataType.SWAPPED_FLOAT32,
            ):
                regs = list(registers[:2])
                if data_type == ModbusDataType.SWAPPED_FLOAT32 or word_order == WordOrder.CDAB:
                    regs[0], regs[1] = regs[1], regs[0]
                packed = struct.pack(">HH", *regs)
                if data_type == ModbusDataType.FLOAT32:
                    return struct.unpack(f"{byte_order.value}f", packed)[0]
                if data_type == ModbusDataType.SWAPPED_FLOAT32:
                    return struct.unpack("<f", packed)[0]
                if data_type == ModbusDataType.UINT32:
                    return struct.unpack(f"{byte_order.value}I", packed)[0]
                return struct.unpack(f"{byte_order.value}i", packed)[0]  # INT32

            if data_type in (
                ModbusDataType.UINT64, ModbusDataType.INT64,
                ModbusDataType.FLOAT64, ModbusDataType.SWAPPED_FLOAT64,
            ):
                regs = list(registers[:4])
                if data_type == ModbusDataType.SWAPPED_FLOAT64 or word_order == WordOrder.CDAB:
                    regs.reverse()
                packed = struct.pack(">HHHH", *regs)
                if data_type == ModbusDataType.FLOAT64:
                    return struct.unpack(f"{byte_order.value}d", packed)[0]
                if data_type == ModbusDataType.SWAPPED_FLOAT64:
                    return struct.unpack("<d", packed)[0]
                if data_type == ModbusDataType.UINT64:
                    return struct.unpack(f"{byte_order.value}Q", packed)[0]
                return struct.unpack(f"{byte_order.value}q", packed)[0]  # INT64

        except Exception:
            pass

        return 0


# ══════════════════════════════════════════════════════════════════════════════
class Register:
    """
    Lightweight value-object representing a single Modbus register address
    and its associated data type.

    Note: actual register *values* are stored in the SlaveConfig sparse data
    dict, not here.  This class is used for address-book / mapping purposes.
    """

    def __init__(self, address: int,
                 data_type: ModbusDataType = ModbusDataType.UINT16):
        self.address   = address
        self.data_type = data_type
        self.value     = 0

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "address":   self.address,
            "data_type": self.data_type.value,
        }
