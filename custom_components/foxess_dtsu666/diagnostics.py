"""Diagnostics support for DTSU666 sniffer."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_SERIAL_PORT
from .coordinator import FoxessCoordinator

_REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: FoxessCoordinator = entry.runtime_data
    s = coordinator.stats

    # Redact connection-identifying info
    safe_data = dict(entry.data)
    for key in (CONF_SERIAL_PORT, CONF_HOST):
        if key in safe_data:
            safe_data[key] = _REDACTED

    all_addrs = set(list(s.addr_timeout) + list(s.addr_crc_err) + list(s.addr_response_ms))

    return {
        "entry_data": safe_data,
        "connected": coordinator.connected,
        "stats": {
            "crc_err_total": len(s.crc_err_times),
            "crc_err_5min": s.count_recent(s.crc_err_times),
            "timeout_total": len(s.timeout_times),
            "timeout_5min": s.count_recent(s.timeout_times),
            "resync_total": len(s.resync_times),
            "resync_5min": s.count_recent(s.resync_times),
            "consecutive_errors": s.consecutive_errors,
            "last_error_at": s.last_error_at,
        },
        "addr_stats": {
            f"0x{addr:04x}": {
                "timeout": s.addr_timeout[addr],
                "crc_err": s.addr_crc_err[addr],
                "avg_poll_interval_ms": (
                    round(sum(s.addr_poll_times[addr]) / len(s.addr_poll_times[addr]) * 1000, 1)
                    if s.addr_poll_times[addr] else None
                ),
                "avg_response_ms": (
                    round(sum(s.addr_response_ms[addr]) / len(s.addr_response_ms[addr]), 1)
                    if s.addr_response_ms[addr] else None
                ),
                "max_response_ms": (
                    max(s.addr_response_ms[addr])
                    if s.addr_response_ms[addr] else None
                ),
            }
            for addr in all_addrs
        },
        "last_seen_ages_s": {
            f"slave=0x{slave_id:02x} addr=0x{addr:04x}": round(time.monotonic() - v, 1)
            for (slave_id, addr), v in coordinator.last_seen.items()
        },
        "unknown_addrs": {
            f"0x{addr:04x}": count
            for addr, count in sorted(s.unknown_addrs.items())
        },
    }
