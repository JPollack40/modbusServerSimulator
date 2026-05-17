"""
simulator_service.py – Facade that owns all running Modbus TCP servers.

This module is the single point of contact between the GUI layer and the
Modbus transport layer.  The GUI calls high-level methods here; this service
translates them into ``ModbusServer`` operations.

Responsibilities
----------------
* Start / stop individual servers or all servers at once.
* Push register-value changes from the GUI model to the live server.
* Populate a freshly started server with the current SlaveConfig data.
* Track which servers are running.

The GUI layer (``MainWindow``) must NOT import ``ModbusServer`` directly;
it should only interact with ``SimulatorService``.
"""

from __future__ import annotations

import logging
from typing import Optional

from models.device_config import (
    ServerConfig, SlaveConfig,
    BOOL_GROUPS, ALL_GROUPS,
    group_to_reg_type,
)
from models.register_data import ModbusDataType, DataConverter, get_register_count
from modbus.server_wrapper import ModbusServer, ModbusServerFactory
from utils.decorators import log_errors_silent

logger = logging.getLogger(__name__)


def _dtype_from_str(text: str) -> ModbusDataType:
    """Convert a data-type display string to a ``ModbusDataType`` enum value."""
    for t in ModbusDataType:
        if t.value == text:
            return t
    return ModbusDataType.UINT16


# ══════════════════════════════════════════════════════════════════════════════
class SimulatorService:
    """
    Facade that manages the lifecycle of all running Modbus TCP servers.

    Usage
    -----
    service = SimulatorService()
    service.start_server(srv_config)          # start one server
    service.push_register(srv, slave, group, row, rd)  # live update
    service.stop_server(srv_config)           # stop one server
    service.stop_all()                        # stop everything
    """

    def __init__(self):
        # id(ServerConfig) → ModbusServer
        self._running: dict[int, ModbusServer] = {}

    # ── Server lifecycle ───────────────────────────────────────────────────────

    @log_errors_silent
    def start_server(self, srv: ServerConfig) -> bool:
        """
        Build, populate, and start a Modbus TCP server for *srv*.

        Returns True on success, False if the server was already running or
        had no slaves configured.
        """
        if self.is_running(srv):
            logger.warning("start_server: %s is already running.", srv.name)
            return False

        if not srv.slaves:
            logger.warning("start_server: %s has no slaves — not starting.", srv.name)
            return False

        ms = ModbusServerFactory.create(srv)

        # Populate register data for every slave
        slaves_by_id: dict[int, list[SlaveConfig]] = {}
        for slave in srv.slaves:
            slaves_by_id.setdefault(slave.slave_id, []).append(slave)

        for slave_id, slave_list in slaves_by_id.items():
            for device_index, slave in enumerate(slave_list):
                self._populate_slave(ms, slave, device_index, srv.zero_based)

        ms.start()
        srv.running = True
        self._running[id(srv)] = ms
        logger.info("Started TCP server '%s' on %s:%d", srv.name, srv.host, srv.port)
        return True

    def stop_server(self, srv: ServerConfig):
        """Stop the running server for *srv* (no-op if not running)."""
        ms = self._running.pop(id(srv), None)
        if ms:
            ms.stop()
            ms.join(timeout=2.0)
        srv.running = False
        logger.info("Stopped TCP server '%s'", srv.name)

    def stop_all(self):
        """Stop all running servers."""
        for key in list(self._running.keys()):
            ms = self._running.pop(key)
            ms.stop()
            ms.join(timeout=2.0)
        # Mark all ServerConfigs as stopped — caller must do this via the project

    def is_running(self, srv: ServerConfig) -> bool:
        """Return True if *srv* has a live server thread."""
        return id(srv) in self._running

    def get_server(self, srv: ServerConfig) -> Optional[ModbusServer]:
        """Return the live ``ModbusServer`` for *srv*, or None."""
        return self._running.get(id(srv))

    # ── Register population ────────────────────────────────────────────────────

    def _populate_slave(self, ms: ModbusServer, slave: SlaveConfig,
                        device_index: int, zero_based: bool):
        """
        Push all non-default register data from *slave* into the live server.

        *device_index* is 0 for the primary device, 1 for the first conflicting
        device, etc.
        """
        for group in ALL_GROUPS:
            reg_type = group_to_reg_type(group)

            if group in BOOL_GROUPS:
                for row, rd in slave.iter_non_default_rows(group):
                    if rd["val"] == "True":
                        ms.update_register_conflict(
                            slave.slave_id, device_index, reg_type, row, 1
                        )
            else:
                skip_until = -1
                for row in sorted(slave.data[group].keys()):
                    if row <= skip_until:
                        continue
                    rd = slave.get_row(group, row)
                    if rd["slave_of"] is not None:
                        continue
                    dtype   = _dtype_from_str(rd["type"])
                    reg_cnt = get_register_count(dtype)
                    raw     = DataConverter.to_registers(rd["val"], dtype)
                    ms.update_registers_conflict(
                        slave.slave_id, device_index, reg_type, row, raw
                    )
                    skip_until = row + reg_cnt - 1

    # ── Live register push (called on every GUI edit) ─────────────────────────

    @log_errors_silent
    def push_register_change(
        self,
        srv: ServerConfig,
        slave: SlaveConfig,
        group: str,
        row: int,
        rd: dict,
    ):
        """
        Push a single register change from the GUI model to the live server.

        Determines the correct device_index by finding *slave*'s position
        among all slaves with the same slave_id on *srv*.
        """
        ms = self._running.get(id(srv))
        if ms is None:
            return

        device_index = self._device_index(srv, slave)
        reg_type     = group_to_reg_type(group)

        if group in BOOL_GROUPS:
            val = 1 if rd["val"] == "True" else 0
            ms.update_register_conflict(slave.slave_id, device_index, reg_type, row, val)
        else:
            if rd.get("slave_of") is not None:
                return
            dtype = _dtype_from_str(rd["type"])
            try:
                raw = DataConverter.to_registers(rd["val"], dtype)
                ms.update_registers_conflict(
                    slave.slave_id, device_index, reg_type, row, raw
                )
            except Exception as exc:
                logger.debug(
                    "push_register_change row=%d slave=%d: %s", row, slave.slave_id, exc
                )

    @log_errors_silent
    def push_dtype_change(
        self,
        srv: ServerConfig,
        slave: SlaveConfig,
        group: str,
        row: int,
        new_type_str: str,
    ):
        """
        Push a data-type change for a master row to the live server.
        Called after the delegate commits a type change.
        """
        ms = self._running.get(id(srv))
        if ms is None:
            return

        device_index = self._device_index(srv, slave)
        reg_type     = group_to_reg_type(group)
        rd           = slave.get_row(group, row)
        dtype        = _dtype_from_str(new_type_str)
        try:
            raw = DataConverter.to_registers(rd["val"], dtype)
            ms.update_registers_conflict(
                slave.slave_id, device_index, reg_type, row, raw
            )
        except Exception as exc:
            logger.debug(
                "push_dtype_change row=%d slave=%d: %s", row, slave.slave_id, exc
            )

    @log_errors_silent
    def set_zero_based(self, srv: ServerConfig, zero_based: bool):
        """Switch addressing mode on the live server for *srv*."""
        ms = self._running.get(id(srv))
        if ms:
            ms.set_zero_based(zero_based)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _device_index(srv: ServerConfig, slave: SlaveConfig) -> int:
        """
        Return the 0-based index of *slave* among all slaves with the same
        slave_id on *srv*.  Returns 0 if not found (safe default).
        """
        peers = srv.get_all_slaves(slave.slave_id)
        try:
            return peers.index(slave)
        except ValueError:
            return 0
