"""Unit tests for the register parser logic."""
import struct
import pytest

# Import directly from registers.py without HA imports by monkey-patching the HA deps.
import sys
import types

# Stub out homeassistant modules so registers.py can be imported standalone.
def _stub_ha():
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorDeviceClass = type("SensorDeviceClass", (), {
        "__getattr__": lambda s, n: n,
    })()
    sensor_mod.SensorStateClass = type("SensorStateClass", (), {
        "__getattr__": lambda s, n: n,
    })()
    const_mod = types.ModuleType("homeassistant.const")
    for name in [
        "UnitOfElectricPotential", "UnitOfElectricCurrent", "UnitOfPower",
        "UnitOfEnergy", "UnitOfFrequency", "UnitOfReactivePower",
    ]:
        obj = type(name, (), {"__getattr__": lambda s, n: n})()
        setattr(const_mod, name, obj)
    helpers = types.ModuleType("homeassistant.helpers")
    entity_mod = types.ModuleType("homeassistant.helpers.entity")
    entity_mod.EntityCategory = type("EntityCategory", (), {"__getattr__": lambda s, n: n})()
    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.sensor", sensor_mod)
    sys.modules.setdefault("homeassistant.const", const_mod)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.entity", entity_mod)

_stub_ha()

# Now import the module under test
import importlib, pathlib, types as _t
spec = importlib.util.spec_from_file_location(
    "registers",
    pathlib.Path(__file__).parent.parent / "custom_components" / "foxess_dtsu666" / "registers.py",
)
registers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registers)

crc16 = registers.crc16
parse_0x1510 = registers.parse_0x1510
parse_0x151e = registers.parse_0x151e
parse_0x181e = registers.parse_0x181e


def _pack_float(v: float) -> bytes:
    return struct.pack(">f", v)


def _make_1510_payload() -> bytes:
    """Craft a minimal parse_0x1510 payload (32 floats = 128 bytes)."""
    values = [float(i) for i in range(32)]
    # Overwrite known positions with test values
    values[0] = 230.1   # voltage_A
    values[3] = 5.0     # current_A
    values[6] = 3.5     # power_total_kW
    values[18] = 0.95   # PFt → pf_total (the corrected mapping)
    values[19] = 0.90   # PFa → pf_A
    values[31] = 50.0   # Freq
    return b"".join(_pack_float(v) for v in values)


def _make_181e_payload() -> bytes:
    """Craft a parse_0x181e payload (10 floats = 40 bytes)."""
    values = [0.0] * 10
    values[0] = 1234.5  # energy_import_total_kWh
    values[5] = 678.9   # energy_export_total_kWh
    return b"".join(_pack_float(v) for v in values)


# ── CRC ─────────────────────────────────────────────────────────────────────

def test_crc16_known_frame():
    # FC03 request: slave=1 FC=03 addr=0x1510 count=64
    frame = bytes([0x01, 0x03, 0x15, 0x10, 0x00, 0x40])
    crc = crc16(frame)
    # Append CRC and verify the whole thing checksums to 0
    frame_with_crc = frame + struct.pack("<H", crc)
    assert crc16(frame_with_crc) == 0

def test_crc16_bad_byte():
    frame = bytes([0x01, 0x03, 0x15, 0x10, 0x00, 0x40])
    crc = crc16(frame)
    frame_with_crc = frame + struct.pack("<H", crc)
    corrupted = bytearray(frame_with_crc)
    corrupted[2] ^= 0xFF
    assert crc16(bytes(corrupted)) != 0


# ── parse_0x1510 ─────────────────────────────────────────────────────────────

def test_parse_0x1510_voltage():
    payload = _make_1510_payload()
    result = parse_0x1510(payload)
    assert abs(result["voltage_A"] - 230.1) < 0.001

def test_parse_0x1510_current():
    payload = _make_1510_payload()
    result = parse_0x1510(payload)
    assert abs(result["current_A"] - 5.0) < 0.001

def test_parse_0x1510_power_total():
    payload = _make_1510_payload()
    result = parse_0x1510(payload)
    assert abs(result["power_total_kW"] - 3.5) < 0.001

def test_parse_0x1510_frequency():
    payload = _make_1510_payload()
    result = parse_0x1510(payload)
    assert abs(result["frequency_Hz"] - 50.0) < 0.001

def test_parse_0x1510_pf_order_regression():
    """Regression: PFt must map to pf_total (f[18]), not pf_A."""
    values = [0.0] * 32
    values[18] = 0.95  # PFt at 0x1534
    values[19] = 0.90  # PFa at 0x1536
    values[20] = 0.85  # PFb at 0x1538
    values[21] = 0.80  # PFc at 0x153A
    payload = b"".join(_pack_float(v) for v in values)
    result = parse_0x1510(payload)
    assert abs(result["pf_total"] - 0.95) < 0.001, "pf_total must be f[18] (PFt)"
    assert abs(result["pf_A"] - 0.90) < 0.001,     "pf_A must be f[19] (PFa)"
    assert abs(result["pf_B"] - 0.85) < 0.001,     "pf_B must be f[20] (PFb)"
    assert abs(result["pf_C"] - 0.80) < 0.001,     "pf_C must be f[21] (PFc)"


# ── parse_0x151e ─────────────────────────────────────────────────────────────

def test_parse_0x151e_power_a():
    payload = b"".join(_pack_float(float(i)) for i in range(7))
    # f[0]=0.0, f[1]=1.0, ..., f[6]=6.0
    result = parse_0x151e(payload)
    assert abs(result["power_A_kW"] - 0.0) < 0.001
    assert abs(result["power_C_kW"] - 2.0) < 0.001
    assert abs(result["reactive_total_kVAr"] - 3.0) < 0.001


# ── parse_0x181e ─────────────────────────────────────────────────────────────

def test_parse_0x181e_import_export():
    payload = _make_181e_payload()
    result = parse_0x181e(payload)
    assert abs(result["energy_import_total_kWh"] - 1234.5) < 0.1
    assert abs(result["energy_export_total_kWh"] - 678.9) < 0.1

def test_parse_0x181e_reactive_Q1_at_f4():
    values = [0.0] * 10
    values[4] = 42.0  # energy_reactive_Q1_kVArh
    payload = b"".join(_pack_float(v) for v in values)
    result = parse_0x181e(payload)
    assert abs(result["energy_reactive_Q1_kVArh"] - 42.0) < 0.001
