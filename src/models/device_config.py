"""
device_config.py – Data-model classes for the multi-server project.

Hierarchy:
    Project
     └── ServerConfig  (unique IP:port, owns one asyncio/thread server)
          └── SlaveConfig  (unique Modbus Server ID within that server)
               └── data  (dict of 4 register groups, each a list of 65536 row-dicts)
"""

from __future__ import annotations
import json
import copy
from dataclasses import dataclass, field
from typing import Optional

from models.register_data import ModbusDataType

# ── Constants ──────────────────────────────────────────────────────────────────
# Full valid Modbus address range: 0x0000–0xFFFF (65536 registers per group)
NUM_ROWS = 65536


def _default_bool_rows() -> list[dict]:
    return [
        {"addr": i, "type": "Boolean", "val": "False", "slave_of": None}
        for i in range(NUM_ROWS)
    ]


def _default_reg_rows() -> list[dict]:
    return [
        {"addr": i, "type": ModbusDataType.UINT16.value, "val": "0", "slave_of": None}
        for i in range(NUM_ROWS)
    ]


def _default_data() -> dict[str, list[dict]]:
    return {
        "Coils":             _default_bool_rows(),
        "Discrete Inputs":   _default_bool_rows(),
        "Holding Registers": _default_reg_rows(),
        "Input Registers":   _default_reg_rows(),
    }


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SlaveConfig:
    """Configuration for a single Modbus slave device."""
    slave_id: int
    data: dict = field(default_factory=_default_data)

    def to_dict(self) -> dict:
        return {
            "slave_id": self.slave_id,
            "registers": copy.deepcopy(self.data),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SlaveConfig":
        sc = cls(slave_id=int(d["slave_id"]))
        if "registers" in d:
            sc.data = d["registers"]
            # Back-fill any missing keys (forward-compat)
            for group, default in _default_data().items():
                if group not in sc.data:
                    sc.data[group] = default
                else:
                    # Ensure each row has a slave_of key
                    for row in sc.data[group]:
                        row.setdefault("slave_of", None)
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

    VERSION = 1

    def __init__(self):
        self.servers: list[ServerConfig] = []

    def add_server(self, server: ServerConfig):
        self.servers.append(server)

    def remove_server(self, server: ServerConfig):
        self.servers = [s for s in self.servers if s is not server]

    def find_conflict(self, host: str, port: int,
                      exclude: Optional[ServerConfig] = None) -> Optional[ServerConfig]:
        """Return the first server that already uses host:port (excluding `exclude`)."""
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
