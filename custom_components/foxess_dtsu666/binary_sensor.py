"""Connectivity binary sensor for DTSU666 sniffer."""
from __future__ import annotations

PARALLEL_UPDATES = 0

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import FoxessCoordinator
from .sensor import _device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FoxessCoordinator = entry.runtime_data
    async_add_entities([Dtsu666ConnectivitySensor(coordinator, entry)])


class Dtsu666ConnectivitySensor(CoordinatorEntity[FoxessCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Bus connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connectivity"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True  # connection sensor is always available so automations can trigger on disconnect

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected
