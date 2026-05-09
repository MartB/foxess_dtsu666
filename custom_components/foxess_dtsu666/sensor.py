"""Sensor entities for DTSU666 measurements and diagnostics."""
from __future__ import annotations

PARALLEL_UPDATES = 0

import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, DOMAIN
from .coordinator import FoxessCoordinator
from .registers import DERIVED_FIELD_DESCRIPTIONS, FIELD_DESCRIPTIONS, compute_cosphi


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FoxessCoordinator = entry.runtime_data

    def _create_slave_entities(slave_id: int) -> None:
        async_add_entities(
            [Dtsu666MeasurementSensor(coordinator, entry, fd, slave_id) for fd in FIELD_DESCRIPTIONS]
            + [Dtsu666DerivedSensor(coordinator, entry, dfd, slave_id) for dfd in DERIVED_FIELD_DESCRIPTIONS]
        )

    coordinator.register_slave_callback(_create_slave_entities)

    # Re-create measurement entities for slaves already seen (after config entry reload)
    for slave_id in coordinator.seen_slaves:
        _create_slave_entities(slave_id)

    # Bus-level diagnostic sensors — always present, not per-slave
    async_add_entities(
        [
            Dtsu666StatSensor(coordinator, entry, "stat_crc_err", "CRC errors (5 min)", None, EntityCategory.DIAGNOSTIC),
            Dtsu666StatSensor(coordinator, entry, "stat_timeout", "Timeouts (5 min)", None, EntityCategory.DIAGNOSTIC),
            Dtsu666StatSensor(coordinator, entry, "stat_resync", "Resyncs (5 min)", None, EntityCategory.DIAGNOSTIC),
            Dtsu666StatSensor(coordinator, entry, "stat_consecutive_errors", "Consecutive errors", None, EntityCategory.DIAGNOSTIC),
            Dtsu666StatSensor(coordinator, entry, "stat_avg_response_ms", "Avg response time", "ms", EntityCategory.DIAGNOSTIC),
            Dtsu666LastErrorSensor(coordinator, entry),
            Dtsu666LastSeenSensor(coordinator, entry),
            Dtsu666AddrPollSensor(coordinator, entry, 0x1510),
            Dtsu666AddrPollSensor(coordinator, entry, 0x151E),
            Dtsu666AddrPollSensor(coordinator, entry, 0x181E),
            Dtsu666DiscoveredAddrsSensor(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry, slave_id: int | None = None) -> DeviceInfo:
    """Return DeviceInfo for the bus (slave_id=None) or a specific slave device."""
    base = entry.data.get(CONF_DEVICE_NAME) or "DTSU666"
    if slave_id is None:
        return DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Chint",
            model="DTSU666",
            name=base,
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{slave_id}")},
        manufacturer="Chint",
        model="DTSU666",
        name=f"{base} #{slave_id}",
        via_device=(DOMAIN, entry.entry_id),
    )


class Dtsu666MeasurementSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry, fd, slave_id: int) -> None:
        super().__init__(coordinator)
        self._fd = fd
        self._slave_id = slave_id
        self._attr_unique_id = f"{entry.entry_id}_{slave_id}_{fd.key}"
        self._attr_translation_key = fd.key
        self._attr_device_class = fd.device_class
        self._attr_state_class = fd.state_class
        self._attr_native_unit_of_measurement = fd.unit
        self._attr_entity_registry_enabled_default = fd.enabled_default
        if fd.entity_category:
            self._attr_entity_category = fd.entity_category
        if fd.precision is not None:
            self._attr_suggested_display_precision = fd.precision
        self._attr_device_info = _device_info(entry, slave_id)

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.is_fresh(self._slave_id, self._fd.addr_key)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get((self._slave_id, self._fd.key))


class Dtsu666StatSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: FoxessCoordinator,
        entry: ConfigEntry,
        stat_key: str,
        name: str,
        unit: str | None,
        entity_category: EntityCategory,
    ) -> None:
        super().__init__(coordinator)
        self._stat_key = stat_key
        self._attr_unique_id = f"{entry.entry_id}_{stat_key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_entity_category = entity_category
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Any:
        s = self.coordinator.stats
        match self._stat_key:
            case "stat_crc_err":
                return s.count_recent(s.crc_err_times)
            case "stat_timeout":
                return s.count_recent(s.timeout_times)
            case "stat_resync":
                return s.count_recent(s.resync_times)
            case "stat_consecutive_errors":
                return s.consecutive_errors
            case "stat_avg_response_ms":
                all_times = [t for q in s.addr_response_ms.values() for t in q]
                return round(sum(all_times) / len(all_times), 1) if all_times else None
        return None


class Dtsu666LastErrorSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "stat_last_error"

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_stat_last_error"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        t = self.coordinator.stats.last_error_at
        return datetime.fromtimestamp(t, tz=timezone.utc) if t is not None else None


class Dtsu666DerivedSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    _attr_has_entity_name = True

    _COMPUTE = {"cosphi_computed": compute_cosphi}

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry, dfd, slave_id: int) -> None:
        super().__init__(coordinator)
        self._dfd = dfd
        self._slave_id = slave_id
        self._attr_unique_id = f"{entry.entry_id}_{slave_id}_{dfd.key}"
        self._attr_translation_key = dfd.key
        self._attr_device_class = dfd.device_class
        self._attr_state_class = dfd.state_class
        self._attr_native_unit_of_measurement = dfd.unit
        self._attr_entity_registry_enabled_default = dfd.enabled_default
        if dfd.precision is not None:
            self._attr_suggested_display_precision = dfd.precision
        self._attr_device_info = _device_info(entry, slave_id)

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        fn = self._COMPUTE.get(self._dfd.key)
        return fn(self.coordinator.data, self._slave_id) if fn else None


class Dtsu666LastSeenSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last seen"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = None
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_seen"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        """Seconds since the most recent successful read across all slaves and register groups."""
        times = list(self.coordinator.last_seen.values())
        if not times:
            return None
        return int(time.monotonic() - max(times))


class Dtsu666AddrPollSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    """Average poll interval for a single Modbus register address."""
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "ms"

    _LABEL = {0x1510: "0x1510", 0x151E: "0x151e", 0x181E: "0x181e"}

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry, addr: int) -> None:
        super().__init__(coordinator)
        self._addr = addr
        self._attr_unique_id = f"{entry.entry_id}_poll_interval_0x{addr:04x}"
        self._attr_translation_key = f"stat_poll_{self._LABEL[addr]}"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        q = self.coordinator.stats.addr_poll_times.get(self._addr)
        if not q:
            return None
        return round(sum(q) / len(q) * 1000, 1)


class Dtsu666DiscoveredAddrsSensor(CoordinatorEntity[FoxessCoordinator], SensorEntity):
    """Lists Modbus addresses seen on the bus that are not handled by this integration."""
    _attr_has_entity_name = True
    _attr_translation_key = "stat_discovered_addrs"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FoxessCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_discovered_addrs"
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        addrs = sorted(self.coordinator.stats.unknown_addrs)
        if not addrs:
            return "none"
        return ", ".join(f"0x{a:04x}" for a in addrs)

    @property
    def extra_state_attributes(self) -> dict:
        return {f"0x{a:04x}": cnt for a, cnt in self.coordinator.stats.unknown_addrs.items()}
