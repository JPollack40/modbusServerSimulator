"""
device_config.py – Data-model classes for the multi-server project.

Hierarchy
---------
Project
 └── ServerConfig  (unique IP:port, owns one asyncio/thread server)
      └── SlaveConfig  (one Modbus slave ID within that server;
                        multiple SlaveConfigs may share the same slave_id
                        to simulate an address conflict)
           └── data  (sparse dict: group → {row_index: row_dict})

Only rows that differ from the default are stored in memory.  Default values
are generated on-the-fly by the helper functions below.

Group mapping helpers (previously in main_window.py) live here because they
are pure domain knowledge, not GUI knowledge.
"""

from __future__ import annotations

import json
import copy
import logging
from dataclasses import dataclass, field
from typing import Optional

from models.register_data import ModbusDataType

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Full valid Modbus address range: 0x0000–0xFFFF (65 536 registers per group)
NUM_ROWS = 65_536

# 5-digit Modbus addressing: each group prefix (0x, 1x, 3x, 4x) allows
# addresses 00001–09999, giving 9 999 usable registers per group.
NUM_ROWS_5DIGIT = 9_999

_DEFAULT_BOOL_TYPE = "Boolean"
_DEFAULT_BOOL_VAL  = "False"
_DEFAULT_REG_TYPE  = ModbusDataType.UINT16.value
_DEFAULT_REG_VAL   = "0"

BOOL_GROUPS = {"Coils", "Discrete Inputs"}
REG_GROUPS  = {"Holding Registers", "Input Registers"}
ALL_GROUPS  = ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]


# ── Group mapping helpers (domain knowledge — not GUI knowledge) ───────────────

def group_addr_offset(group: str, zero_based: bool) -> int:
    """Return the display address offset for a register group."""
    if zero_based:
        return {
            "Coils":             0,
            "Discrete Inputs":   10_000,
            "Holding Registers": 40_000,
            "Input Registers":   30_000,
        }[group]
    return {
        "Coils":             1,
        "Discrete Inputs":   10_001,
        "Holding Registers": 40_001,
        "Input Registers":   30_001,
    }[group]


def group_to_reg_type(group: str) -> str:
    """Map a human-readable group name to the pymodbus register-type key."""
    return {
        "Coils":             "coils",
        "Discrete Inputs":   "discrete_inputs",
        "Holding Registers": "holding_registers",
        "Input Registers":   "input_registers",
    }[group]


# ── Row helpers ────────────────────────────────────────────────────────────────

def default_bool_row(addr: int) -> dict:
    """Return a default Boolean row dict."""
    return {"addr": addr, "type": _DEFAULT_BOOL_TYPE,
            "val": _DEFAULT_BOOL_VAL, "slave_of": None}


def default_reg_row(addr: int) -> dict:
    """Return a default register row dict."""
    return {"addr": addr, "type": _DEFAULT_REG_TYPE,
            "val": _DEFAULT_REG_VAL, "slave_of": None}


def default_row(addr: int, group: str) -> dict:
    """Return a default row dict for the given group."""
    return default_bool_row(addr) if group in BOOL_GROUPS else default_reg_row(addr)


def is_default_row(row: dict, group: str) -> bool:
    """Return True if the row holds only default values (can be omitted from storage)."""
    if group in BOOL_GROUPS:
        return row["val"] == _DEFAULT_BOOL_VAL and row["slave_of"] is None
    return (
        row["type"] == _DEFAULT_REG_TYPE
        and row["val"] == _DEFAULT_REG_VAL
        and row["slave_of"] is None
    )


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

    Multiple SlaveConfig objects may share the same ``slave_id`` on a
    ServerConfig to simulate an RS-485 address conflict.
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
        """Serialise to a JSON-compatible dict (only non-default rows stored)."""
        serialised: dict[str, list] = {}
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
        """
        Deserialise from a dict.  Handles both the current sparse-list format
        and the legacy dense-dict format.  Malformed individual entries are
        skipped with a warning rather than aborting the entire load.
        """
        sc = cls(slave_id=int(d["slave_id"]))
        raw = d.get("registers", {})

        for group in ALL_GROUPS:
            entries = raw.get(group)
            if entries is None:
                continue

            if isinstance(entries, list):
                # Current sparse format: list of {"row": N, ...}
                for entry in entries:
                    try:
                        entry = dict(entry)          # copy before mutating
                        row = int(entry.pop("row", entry.get("addr", 0)))
                        entry.setdefault("addr", row)
                        entry.setdefault("slave_of", None)
                        entry.setdefault("type", _DEFAULT_REG_TYPE
                                         if group not in BOOL_GROUPS
                                         else _DEFAULT_BOOL_TYPE)
                        entry.setdefault("val", _DEFAULT_REG_VAL
                                         if group not in BOOL_GROUPS
                                         else _DEFAULT_BOOL_VAL)
                        sc.data[group][row] = entry
                    except Exception as exc:
                        logger.warning(
                            "SlaveConfig.from_dict: skipping malformed entry "
                            "in group '%s': %s — %s", group, entry, exc
                        )

            elif isinstance(entries, dict):
                # Legacy dense format: dict keyed by str index
                for k, v in entries.items():
                    try:
                        row = int(k)
                        v = dict(v)
                        v.setdefault("addr", row)
                        v.setdefault("slave_of", None)
                        v.setdefault("type", _DEFAULT_REG_TYPE
                                     if group not in BOOL_GROUPS
                                     else _DEFAULT_BOOL_TYPE)
                        v.setdefault("val", _DEFAULT_REG_VAL
                                     if group not in BOOL_GROUPS
                                     else _DEFAULT_BOOL_VAL)
                        sc.data[group][row] = v
                    except Exception as exc:
                        logger.warning(
                            "SlaveConfig.from_dict: skipping legacy entry "
                            "key='%s' in group '%s': %s", k, group, exc
                        )
            else:
                logger.warning(
                    "SlaveConfig.from_dict: unexpected type %s for group '%s'; skipping.",
                    type(entries).__name__, group,
                )

        return sc

    def clone(self) -> "SlaveConfig":
        """Return a deep copy of this SlaveConfig."""
        return SlaveConfig(
            slave_id=self.slave_id,
            data=copy.deepcopy(self.data),
        )


# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ServerConfig:
    """
    Configuration for one TCP Modbus server (unique IP:port).

    A ServerConfig may contain multiple SlaveConfig objects with the same
    ``slave_id`` to simulate an RS-485 address conflict.
    """

    name: str = "New Server"
    host: str = "0.0.0.0"
    port: int = 502
    zero_based: bool = False
    six_digit: bool = False   # False = 5-digit (9 999 regs), True = 6-digit (65 536 regs)
    slaves: list[SlaveConfig] = field(default_factory=list)

    # Runtime state — not serialised
    running: bool = field(default=False, repr=False)

    # ── Slave accessors ────────────────────────────────────────────────────────

    def get_slave(self, slave_id: int) -> Optional[SlaveConfig]:
        """Return the first SlaveConfig with the given ID, or None."""
        for s in self.slaves:
            if s.slave_id == slave_id:
                return s
        return None

    def get_all_slaves(self, slave_id: int) -> list[SlaveConfig]:
        """Return all SlaveConfigs with the given ID (may be >1 for conflicts)."""
        return [s for s in self.slaves if s.slave_id == slave_id]

    def has_conflict(self, slave_id: int) -> bool:
        """Return True if more than one slave shares *slave_id* (address conflict)."""
        return len(self.get_all_slaves(slave_id)) > 1

    def conflicting_ids(self) -> set[int]:
        """Return the set of slave IDs that appear more than once."""
        seen: set[int] = set()
        conflicts: set[int] = set()
        for s in self.slaves:
            if s.slave_id in seen:
                conflicts.add(s.slave_id)
            seen.add(s.slave_id)
        return conflicts

    def add_slave(self, slave_id: int,
                  allow_duplicate: bool = False) -> SlaveConfig:
        """
        Add a new SlaveConfig.

        Parameters
        ----------
        slave_id : int
            Modbus slave / unit ID (1–247).
        allow_duplicate : bool
            When False (default) a ValueError is raised if *slave_id* already
            exists.  When True the duplicate is allowed — this simulates an
            RS-485 address conflict between two physical devices.
        """
        if not allow_duplicate and self.get_slave(slave_id) is not None:
            raise ValueError(f"Slave ID {slave_id} already exists on this server.")
        sc = SlaveConfig(slave_id=slave_id)
        self.slaves.append(sc)
        self.slaves.sort(key=lambda s: s.slave_id)
        return sc

    def remove_slave(self, slave_id: int,
                     instance: Optional[SlaveConfig] = None) -> bool:
        """
        Remove a slave by ID.

        If *instance* is provided, only that specific object is removed
        (useful when multiple slaves share the same ID).  Otherwise the
        first match is removed.
        """
        before = len(self.slaves)
        if instance is not None:
            self.slaves = [s for s in self.slaves if s is not instance]
        else:
            removed = False
            new_list = []
            for s in self.slaves:
                if s.slave_id == slave_id and not removed:
                    removed = True   # skip first match
                else:
                    new_list.append(s)
            self.slaves = new_list
        return len(self.slaves) < before

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "name":       self.name,
            "host":       self.host,
            "port":       self.port,
            "zero_based": self.zero_based,
            "six_digit":  self.six_digit,
            "slaves":     [s.to_dict() for s in self.slaves],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ServerConfig":
        """Deserialise from a dict.  Malformed slaves are skipped with a warning."""
        sc = cls(
            name       = d.get("name", "Server"),
            host       = d.get("host", "0.0.0.0"),
            port       = int(d.get("port", 502)),
            zero_based = bool(d.get("zero_based", False)),
            six_digit  = bool(d.get("six_digit", False)),
        )
        for sd in d.get("slaves", []):
            try:
                sc.slaves.append(SlaveConfig.from_dict(sd))
            except Exception as exc:
                logger.warning(
                    "ServerConfig.from_dict: skipping malformed slave %s: %s",
                    sd, exc,
                )
        return sc

    def save_to_file(self, path: str):
        """Serialise and write to a JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, path: str) -> "ServerConfig":
        """Load a ServerConfig from a JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ══════════════════════════════════════════════════════════════════════════════
class Project:
    """Top-level container for the entire multi-server setup."""

    VERSION = 2   # bumped for sparse-data format

    def __init__(self):
        self.servers: list[ServerConfig] = []

    # ── Server management ──────────────────────────────────────────────────────

    def add_server(self, server: ServerConfig):
        """Append a ServerConfig to the project."""
        self.servers.append(server)

    def remove_server(self, server: ServerConfig):
        """Remove a ServerConfig by identity."""
        self.servers = [s for s in self.servers if s is not server]

    def find_conflict(self, host: str, port: int,
                      exclude: Optional[ServerConfig] = None
                      ) -> Optional[ServerConfig]:
        """
        Return the first ServerConfig that uses the same host:port, or None.
        Pass *exclude* to ignore a specific server (e.g. when editing it).
        """
        for s in self.servers:
            if s is exclude:
                continue
            if s.host == host and s.port == port:
                return s
        return None

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "version": self.VERSION,
            "servers": [s.to_dict() for s in self.servers],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        """
        Deserialise from a dict.  Each server is loaded independently so that
        a malformed server does not abort the entire project load.
        """
        p = cls()
        for sd in d.get("servers", []):
            try:
                p.servers.append(ServerConfig.from_dict(sd))
            except Exception as exc:
                logger.warning(
                    "Project.from_dict: skipping malformed server %s: %s",
                    sd.get("name", "<unknown>"), exc,
                )
        return p

    def save_to_file(self, path: str):
        """Serialise and write to a JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, path: str) -> "Project":
        """
        Load a Project from a JSON file.

        Raises
        ------
        ValueError
            If the file cannot be parsed as JSON or the top-level structure is
            invalid.  Individual malformed servers/slaves are skipped with
            warnings rather than raising.
        """
        with open(path, "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in project file: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError("Project file must contain a JSON object at the top level.")

        return cls.from_dict(raw)
