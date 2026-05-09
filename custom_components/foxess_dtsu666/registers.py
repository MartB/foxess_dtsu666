import struct
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.helpers.entity import EntityCategory


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _floats(data: bytes, offset: int, count: int) -> list[float]:
    return [
        struct.unpack(">f", data[offset + i * 4 : offset + i * 4 + 4])[0]
        for i in range(count)
    ]


def parse_0x1510(payload: bytes) -> dict:
    f = _floats(payload, 0, 32)
    return {
        "voltage_A": f[0],
        "voltage_B": f[1],
        "voltage_C": f[2],
        "current_A": f[3],
        "current_B": f[4],
        "current_C": f[5],
        "power_total_kW": f[6],
        "power_A_kW": f[7],
        "power_B_kW": f[8],
        "power_C_kW": f[9],
        "reactive_total_kVAr": f[10],
        "reactive_A_kVAr": f[11],
        "reactive_B_kVAr": f[12],
        "reactive_C_kVAr": f[13],
        "apparent_total_kVA": f[14],
        "apparent_A_kVA": f[15],
        "apparent_B_kVA": f[16],
        "apparent_C_kVA": f[17],
        # f[18]=0x1534 PFt, f[19]=0x1536 PFa, f[20]=0x1538 PFb, f[21]=0x153A PFc
        "pf_total": f[18],
        "pf_A": f[19],
        "pf_B": f[20],
        "pf_C": f[21],
        "frequency_Hz": f[31],
    }


def parse_0x151e(payload: bytes) -> dict:
    f = _floats(payload, 0, 7)
    return {
        "power_A_kW": f[0],
        "power_B_kW": f[1],
        "power_C_kW": f[2],
        "reactive_total_kVAr": f[3],
        "reactive_A_kVAr": f[4],
        "reactive_B_kVAr": f[5],
        "reactive_C_kVAr": f[6],
    }


def parse_0x181e(payload: bytes) -> dict:
    f = _floats(payload, 0, 10)
    return {
        "energy_import_total_kWh": f[0],
        "energy_import_A_kWh": f[1],
        "energy_import_B_kWh": f[2],
        "energy_import_C_kWh": f[3],
        "energy_reactive_Q1_kVArh": f[4],
        "energy_export_total_kWh": f[5],
        "energy_export_A_kWh": f[6],
        "energy_export_B_kWh": f[7],
        "energy_export_C_kWh": f[8],
        "energy_reactive_total_kVArh": f[9],
    }


# Maps register start address → (register_count, parser, addr_key)
PARSERS: dict[int, tuple[int, callable, int]] = {
    0x1510: (64, parse_0x1510, 0x1510),
    0x151E: (14, parse_0x151e, 0x151E),
    0x181E: (20, parse_0x181e, 0x181E),
}


class _DerivedFD:
    """Descriptor for a sensor computed from other fields already in coordinator data."""

    __slots__ = ("key", "device_class", "state_class", "unit", "enabled_default", "precision")

    def __init__(self, key, device_class, state_class, unit, enabled_default=True, precision=None):
        self.key = key
        self.device_class = device_class
        self.state_class = state_class
        self.unit = unit
        self.enabled_default = enabled_default
        self.precision = precision


def compute_cosphi(data: dict, slave_id: int) -> float | None:
    import math
    p = data.get((slave_id, "power_total_kW"))
    q = data.get((slave_id, "reactive_total_kVAr"))
    if p is None or q is None:
        return None
    s2 = p * p + q * q
    return round(abs(p) / math.sqrt(s2), 4) if s2 > 0.0 else None


class _FD:
    """Compact field descriptor."""

    __slots__ = ("key", "device_class", "state_class", "unit", "entity_category", "enabled_default", "addr_key", "precision")

    def __init__(self, key, device_class, state_class, unit, addr_key, enabled_default=True, entity_category=None, precision=None):
        self.key = key
        self.device_class = device_class
        self.state_class = state_class
        self.unit = unit
        self.addr_key = addr_key
        self.enabled_default = enabled_default
        self.entity_category = entity_category
        self.precision = precision


def _three_phase(prefix, suffix, dc, sc, unit, addr_key, *, enabled_default=True, precision=None) -> list[_FD]:
    """Return _FD entries for phases A, B, C."""
    return [
        _FD(f"{prefix}_A{suffix}", dc, sc, unit, addr_key, enabled_default, precision=precision),
        _FD(f"{prefix}_B{suffix}", dc, sc, unit, addr_key, enabled_default, precision=precision),
        _FD(f"{prefix}_C{suffix}", dc, sc, unit, addr_key, enabled_default, precision=precision),
    ]


_DC  = SensorDeviceClass
_M   = SensorStateClass.MEASUREMENT
_TI  = SensorStateClass.TOTAL_INCREASING
_V   = UnitOfElectricPotential.VOLT
_A   = UnitOfElectricCurrent.AMPERE
_kW  = UnitOfPower.KILO_WATT
_kWh = UnitOfEnergy.KILO_WATT_HOUR
_Hz  = UnitOfFrequency.HERTZ
_kVAr = "kvar"   # UnitOfReactivePower.KILO_VOLT_AMPERE_REACTIVE

FIELD_DESCRIPTIONS: list[_FD] = [
    # Voltages
    *_three_phase("voltage",  "",     _DC.VOLTAGE,         _M,  _V,    0x1510),
    # Currents
    *_three_phase("current",  "",     _DC.CURRENT,         _M,  _A,    0x1510),
    # Active power — total from 0x1510, per-phase from 0x151E (polled separately)
    _FD("power_total_kW",             _DC.POWER,           _M,  _kW,   0x1510),
    *_three_phase("power",    "_kW",  _DC.POWER,           _M,  _kW,   0x151E),
    # Reactive power
    _FD("reactive_total_kVAr",        _DC.REACTIVE_POWER,  _M,  _kVAr, 0x151E, enabled_default=False),
    *_three_phase("reactive", "_kVAr",_DC.REACTIVE_POWER,  _M,  _kVAr, 0x151E, enabled_default=False),
    # Apparent power
    _FD("apparent_total_kVA",         _DC.APPARENT_POWER,  _M,  "kVA", 0x1510),
    *_three_phase("apparent", "_kVA", _DC.APPARENT_POWER,  _M,  "kVA", 0x1510),
    # Power factor (unit=None per HA convention; disabled by default)
    _FD("pf_total",                   _DC.POWER_FACTOR,    _M,  None,  0x1510, enabled_default=False, precision=3),
    *_three_phase("pf",       "",     _DC.POWER_FACTOR,    _M,  None,  0x1510, enabled_default=False, precision=3),
    # Frequency
    _FD("frequency_Hz",               _DC.FREQUENCY,       _M,  _Hz,   0x1510, precision=2),
    # Energy — totals on by default, per-phase off
    _FD("energy_import_total_kWh",    _DC.ENERGY,          _TI, _kWh,  0x181E),
    _FD("energy_export_total_kWh",    _DC.ENERGY,          _TI, _kWh,  0x181E),
    *_three_phase("energy_import", "_kWh", _DC.ENERGY,     _TI, _kWh,  0x181E, enabled_default=False),
    *_three_phase("energy_export", "_kWh", _DC.ENERGY,     _TI, _kWh,  0x181E, enabled_default=False),
    # Reactive energy (no HA device class for kVArh)
    _FD("energy_reactive_Q1_kVArh",   None,                _TI, "kVArh", 0x181E, enabled_default=False, precision=2),
    _FD("energy_reactive_total_kVArh", None,               _TI, "kVArh", 0x181E, precision=2),
]

DERIVED_FIELD_DESCRIPTIONS: list[_DerivedFD] = [
    _DerivedFD("cosphi_computed", _DC.POWER_FACTOR, _M, None, precision=3),
]
