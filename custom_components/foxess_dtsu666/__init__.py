"""FoxESS DTSU666 passive Modbus RTU sniffer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

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

# Keys which were read under the wrong name, old -> new. 0x1826/0x1830 were taken for
# reactive energy, and are the net energy totals; see parse_0x181e. Migrating the registry
# entries rather than creating new ones keeps their long-term statistics, which the recorder
# follows through an entity_id rename.
_RENAMED_KEYS = {
    "energy_reactive_Q1_kVArh": ("energy_net_import_total_kWh", "reactive_energy_q1", "net_energy_import_total"),
    "energy_reactive_total_kVArh": ("energy_net_export_total_kWh", "reactive_energy_total", "net_energy_export_total"),
}


async def _async_migrate_renamed_keys(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)

    @callback
    def _migrate(entity_entry: er.RegistryEntry) -> dict | None:
        for old, (new, old_slug, new_slug) in _RENAMED_KEYS.items():
            if not entity_entry.unique_id.endswith(f"_{old}"):
                continue
            updates: dict = {"new_unique_id": entity_entry.unique_id[: -len(old)] + new}
            # The entity_id was made from the old name. Follow the new one where it's free
            new_entity_id = entity_entry.entity_id.replace(old_slug, new_slug)
            if new_entity_id != entity_entry.entity_id and not registry.async_is_registered(new_entity_id):
                updates["new_entity_id"] = new_entity_id
            # The net import total is on by default now, where the "Q1" entry never was
            if entity_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                updates["disabled_by"] = None
            _LOGGER.info("Migrating %s to %s", entity_entry.entity_id, updates)
            return updates
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    conn_type = data.get(CONF_CONNECTION_TYPE, CONN_SERIAL)

    if conn_type == CONN_SERIAL:
        sniffer_kwargs = dict(
            serial_port=data[CONF_SERIAL_PORT],
            baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
            parity=data.get(CONF_PARITY, DEFAULT_PARITY).upper(),
            bytesize=data.get(CONF_BYTESIZE, DEFAULT_BYTESIZE),
            stopbits=data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
        )
    else:
        sniffer_kwargs = dict(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
        )

    await _async_migrate_renamed_keys(hass, entry)

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
