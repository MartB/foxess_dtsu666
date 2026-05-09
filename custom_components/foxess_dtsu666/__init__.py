"""FoxESS DTSU666 passive Modbus RTU sniffer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALWAYS_POLL,
    CONF_BAUDRATE,
    CONF_BYTESIZE,
    CONF_CONNECTION_TYPE,
    CONF_HOST,
    CONF_PARITY,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLAVE_ID,
    CONF_STOPBITS,
    CONN_SERIAL,
    DEFAULT_BAUDRATE,
    DEFAULT_BYTESIZE,
    DEFAULT_PARITY,
    DEFAULT_STOPBITS,
)
from .coordinator import FoxessCoordinator
from .sniffer import ModbusRtuSniffer

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    conn_type = data.get(CONF_CONNECTION_TYPE, CONN_SERIAL)

    if conn_type == CONN_SERIAL:
        sniffer_kwargs = dict(
            serial_port=data[CONF_SERIAL_PORT],
            baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
            parity=data.get(CONF_PARITY, DEFAULT_PARITY),
            bytesize=data.get(CONF_BYTESIZE, DEFAULT_BYTESIZE),
            stopbits=data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
        )
    else:
        sniffer_kwargs = dict(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
        )

    coordinator: FoxessCoordinator | None = None

    slave_id = int(data.get(CONF_SLAVE_ID, 0)) or None
    always_poll = bool(data.get(CONF_ALWAYS_POLL, False))

    sniffer = ModbusRtuSniffer(
        on_response=lambda slave_id, addr, parsed: coordinator.on_response(slave_id, addr, parsed),
        on_status=lambda status: coordinator.on_status(status),
        initial_slave_id=slave_id,
        always_poll=always_poll,
        **sniffer_kwargs,
    )

    coordinator = FoxessCoordinator(hass, entry, sniffer)

    entry.runtime_data = coordinator

    # Start the sniffer as a background task owned by the config entry
    entry.async_create_background_task(hass, sniffer.run(), name=f"dtsu666_sniffer_{entry.entry_id}")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: FoxessCoordinator = entry.runtime_data
        coordinator._cancel_no_traffic_timer()
        coordinator._sniffer.stop()
        # Background tasks created with async_create_background_task are automatically
        # cancelled when the entry is unloaded, but we call stop() to close the transport.
    return ok
