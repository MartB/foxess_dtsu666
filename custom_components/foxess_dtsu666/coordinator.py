"""Push-based coordinator for DTSU666 sniffer data."""
from __future__ import annotations

import logging
import time
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_STALENESS, CONF_UPDATE_INTERVAL, DEFAULT_STALENESS, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .sniffer import ModbusRtuSniffer, SnifferStats, SnifferStatus

_LOGGER = logging.getLogger(__name__)

_NO_TRAFFIC_DELAY = 30  # seconds after connect before raising the repair


class FoxessCoordinator(DataUpdateCoordinator[dict]):
    """Receives data pushed by ModbusRtuSniffer and notifies entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sniffer: ModbusRtuSniffer) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self._entry = entry
        self._sniffer = sniffer
        self.staleness: float = float(entry.options.get(CONF_STALENESS, entry.data.get(CONF_STALENESS, DEFAULT_STALENESS)))
        self._update_interval: float = float(entry.options.get(CONF_UPDATE_INTERVAL, entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        # Keyed by (slave_id, register_addr) — energy and instantaneous groups poll at different rates
        self.last_seen: dict[tuple[int, int], float] = {}
        self.connected: bool = False
        # Flat store of latest decoded values, keyed by (slave_id, field_name)
        self._store: dict[tuple[int, str], float] = {}
        self._last_push: float = 0.0
        # Auto-discovered slave IDs (only added after a valid response is decoded)
        self.seen_slaves: set[int] = set()
        self._slave_callbacks: list[Callable[[int], None]] = []
        # Cancel callback for the pending no-traffic timer (if any)
        self._no_traffic_cancel: Callable | None = None

    @property
    def stats(self) -> SnifferStats:
        return self._sniffer.stats

    def register_slave_callback(self, cb: Callable[[int], None]) -> None:
        """Register a callback fired once per newly-discovered slave."""
        self._slave_callbacks.append(cb)

    def on_response(self, slave_id: int, addr: int, parsed: dict) -> None:
        """Called from the sniffer task when a valid frame is decoded."""
        now = time.monotonic()
        self.last_seen[(slave_id, addr)] = now
        for key, val in parsed.items():
            self._store[(slave_id, key)] = val

        if slave_id not in self.seen_slaves:
            self.seen_slaves.add(slave_id)
            _LOGGER.info(
                "DTSU666 discovered slave 0x%02x. To enable active polling when the inverter is offline, "
                "set Slave ID to %d in the integration options.",
                slave_id, slave_id,
            )
            for cb in self._slave_callbacks:
                cb(slave_id)

        # Cancel any pending no-traffic timer and clear the repair
        self._cancel_no_traffic_timer()
        ir.async_delete_issue(self.hass, DOMAIN, f"no_traffic_{self._entry.entry_id}")

        # Throttle entity pushes — _store and last_seen are always current for staleness checks
        if now - self._last_push >= self._update_interval:
            self._last_push = now
            self.async_set_updated_data(dict(self._store))

    def on_status(self, status: SnifferStatus) -> None:
        prev = self.connected
        self.connected = status == SnifferStatus.CONNECTED
        if self.connected != prev:
            if self.connected:
                _LOGGER.info("DTSU666 bus connection restored")
                # Schedule a no-traffic check; cancelled immediately if any frame arrives
                self._cancel_no_traffic_timer()
                self._no_traffic_cancel = async_call_later(
                    self.hass, _NO_TRAFFIC_DELAY, self._warn_if_no_traffic
                )
            else:
                self._cancel_no_traffic_timer()
                _LOGGER.warning("DTSU666 bus connection lost (status=%s)", status.value)
            if self.data is not None:
                self.async_set_updated_data(dict(self._store))

    @callback
    def _cancel_no_traffic_timer(self) -> None:
        if self._no_traffic_cancel is not None:
            self._no_traffic_cancel()
            self._no_traffic_cancel = None

    @callback
    def _warn_if_no_traffic(self, _now=None) -> None:
        self._no_traffic_cancel = None
        if not self.connected or self.seen_slaves:
            return

        s = self.stats
        if s.total == 0:
            translation_key = "no_traffic_silent"
            _LOGGER.warning(
                "DTSU666 sniffer: no bytes received after %ds — check wiring and serial port selection",
                _NO_TRAFFIC_DELAY,
            )
        elif s.ok == 0:
            translation_key = "no_traffic_crc"
            _LOGGER.warning(
                "DTSU666 sniffer: %d frames received but all failing CRC after %ds — "
                "check baud rate (%d) and parity setting",
                s.total, _NO_TRAFFIC_DELAY, self._sniffer._baudrate,
            )
        else:
            translation_key = "no_traffic_unknown"
            _LOGGER.warning(
                "DTSU666 sniffer: %d valid frames seen but no recognised register addresses after %ds",
                s.ok, _NO_TRAFFIC_DELAY,
            )

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"no_traffic_{self._entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={"name": self._entry.title},
        )

    def is_fresh(self, slave_id: int, addr_key: int) -> bool:
        """True if the register group at addr_key for this slave was seen recently."""
        ts = self.last_seen.get((slave_id, addr_key))
        return ts is not None and (time.monotonic() - ts) < self.staleness

    async def _async_update_data(self) -> dict:
        # DataUpdateCoordinator requires this; never called (push model, no update_interval).
        return dict(self._store)
