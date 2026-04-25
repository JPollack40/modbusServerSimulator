"""
device_config.py – Data-model classes for the multi-server project.

Hierarchy:
    Project
     └── ServerConfig  (unique IP:port, owns one asyncio/thread server)
          └── SlaveConfig  (unique Modbus Server ID within that server)
               └── data  (dict of 4 register groups, each a SPARSE dict:
                           row_index → {"addr", "type", "val", "slave_of"})

The full valid Modbus address range is 0x0000–0xFFFF (65 536 registers per
group).  We store only the rows that differ from the default so that memory
usage stays reasonable.  Default values are generated on-the-fly by helpers.
"""

from __future__ import annotations
import json
import copy
from dataclasses import dataclass, field
from typing import Optional

from models.register_data import ModbusDataType

# ── Constants ──────────────────────────────────────────────────────────────────
# Full valid Modbus address range: 0x0000–0xFFFF (65 536 registers per group)
NUM_ROWS = 65536

_DEFAULT_BOOL_TYPE = "Boolean"
_DEFAULT_BOOL_VAL  = "False"
_DEFAULT_REG_TYPE  = ModbusDataType.UINT16.value
_DEFAULT_REG_VAL   = "0"

BOOL_GROUPS = {"Coils", "Discrete Inputs"}
REG_GROUPS  = {"Holding Registers", "Input Registers"}
ALL_GROUPS  = list(BOOL_GROUPS | REG_GROUPS)


# ── Row helpers ────────────────────────────────────────────────────────────────

def default_bool_row(addr: int) -> dict:
    return {"addr": addr, "type": _DEFAULT_BOOL_TYPE,
            "val": _DEFAULT_BOOL_VAL, "slave_of": None}


def default_reg_row(addr: int) -> dict:
    return {"addr": addr, "type": _DEFAULT_REG_TYPE,
            "val": _DEFAULT_REG_VAL, "slave_of": None}


def default_row(addr: int, group: str) -> dict:
    return default_bool_row(addr) if group in BOOL_GROUPS else default_reg_row(addr)


def is_default_row(row: dict, group: str) -> bool:
    """Return True if the row holds only default values (can be omitted from storage)."""
    if group in BOOL_GROUPS:
        return (row["val"] == _DEFAULT_BOOL_VAL and row["slave_of"] is None)
    else:
        return (row["type"] == _DEFAULT_REG_TYPE and
                row["val"]  == _DEFAULT_REG_VAL  and
                row["slave_of"] is None)


def _empty_sparse_data() -> dict[str, dict]:
    """Return an empty sparse data structure (no rows stored yet)."""
    return {g: {} for g in ALL_GROUPS}


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SlaveConfig:
    """
    Configuration for a single Modbus slave device.

    ``data`` is a sparse dict:
        group_name → { row_index(int) → row_dict }

    Rows that hold only default values are NOT stored; callers must use
    ``get_row()`` / ``set_row()`` rather than direct dict access.
    """
    slave_id: int
    # sparse: group → {row_index: row_dict}
    data: dict = field(default_factory=_empty_sparse_data)

    # ── Row accessors ──────────────────────────────────────────────────────────

    def get_row(self, group: str, row: int) -> dict:
        """Return the row dict for (group, row), generating a default if absent."""
        return self.data[group].get(row, default_row(row, group))

    def set_row(self, group: str, row: int, row_dict: dict):
        """Store a row dict.  If it equals the default, remove it to save memory."""
        if is_default_row(row_dict, group):
            self.data[group].pop(row, None)
        else:
            self.data[group][row] = row_dict

    def iter_non_default_rows(self, group: str):
        """Yield (row_index, row_dict) for all non-default rows in a group."""
        yield from self.data[group].items()

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        # Only serialise non-default rows
        serialised = {}
        for group, rows in self.data.items():
            serialised[group] = [
                {"row": row, **rd} for row, rd in sorted(rows.items())
            ]
        return {
            "slave_id":  self.slave_id,
            "registers": serialised,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SlaveConfig":
        sc = cls(slave_id=int(d["slave_id"]))
        if "registers" in d:
            raw = d["registers"]
            for group in ALL_GROUPS:
                if group not in raw:
                    continue
                entries = raw[group]
                if isinstance(entries, list):
                    # New sparse format: list of {"row": N, ...}
                    for entry in entries:
                        entry = dict(entry)
                        row = int(entry.pop("row", entry.get("addr", 0)))
                        entry.setdefault("addr", row)
                        entry.setdefault("slave_of", None)
                        sc.data[group][row] = entry
                elif isinstance(entries, dict):
                    # Legacy dense format saved as dict keyed by str index
                    for k, v in entries.items():
                        row = int(k)
                        v = dict(v)
                        v.setdefault("addr", row)
                        v.setdefault("slave_of", None)
                        sc.data[group][row] = v
        return sc

    def clone(self) -> "SlaveConfig":
        return SlaveConfig(
            slave_id=self.slave_id,
            data=copy.deepcopy(self.data),
        )


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ServerConfig:
    """Configuration for one TCP Modbus server (unique IP:port)."""
    name: str = "New Server"
    host: str = "0.0.0.0"
    port: int = 502
    zero_based: bool = False
    slaves: list[SlaveConfig] = field(default_factory=list)

    # Runtime state (not serialised)
    running: bool = field(default=False, repr=False)

    def get_slave(self, slave_id: int) -> Optional[SlaveConfig]:
        for s in self.slaves:
            if s.slave_id == slave_id:
                return s
        return None

    def add_slave(self, slave_id: int) -> SlaveConfig:
        if self.get_slave(slave_id) is not None:
            raise ValueError(f"Slave ID {slave_id} already exists on this server.")
        sc = SlaveConfig(slave_id=slave_id)
        self.slaves.append(sc)
        self.slaves.sort(key=lambda s: s.slave_id)
        return sc

    def remove_slave(self, slave_id: int) -> bool:
        before = len(self.slaves)
        self.slaves = [s for s in self.slaves if s.slave_id != slave_id]
        return len(self.slaves) < before

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "host":       self.host,
            "port":       self.port,
            "zero_based": self.zero_based,
            "slaves":     [s.to_dict() for s in self.slaves],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ServerConfig":
        sc = cls(
            name       = d.get("name", "Server"),
            host       = d.get("host", "0.0.0.0"),
            port       = int(d.get("port", 502)),
            zero_based = bool(d.get("zero_based", False)),
        )
        for sd in d.get("slaves", []):
            sc.slaves.append(SlaveConfig.from_dict(sd))
        return sc

    def save_to_file(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, path: str) -> "ServerConfig":
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))


# ══════════════════════════════════════════════════════════════════════════════
class Project:
    """Top-level container for the entire multi-server setup."""

    VERSION = 2   # bumped for sparse-data format

    def __init__(self):
        self.servers: list[ServerConfig] = []

    def add_server(self, server: ServerConfig):
        self.servers.append(server)

    def remove_server(self, server: ServerConfig):
        self.servers = [s for s in self.servers if s is not server]

    def find_conflict(self, host: str, port: int,
                      exclude: Optional[ServerConfig] = None) -> Optional[ServerConfig]:
        for s in self.servers:
            if s is exclude:
                continue
            if s.host == host and s.port == port:
                return s
        return None

    def to_dict(self) -> dict:
        return {
            "version": self.VERSION,
            "servers": [s.to_dict() for s in self.servers],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        p = cls()
        for sd in d.get("servers", []):
            p.servers.append(ServerConfig.from_dict(sd))
        return p

    def save_to_file(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, path: str) -> "Project":
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))
