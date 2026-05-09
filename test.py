import serial, time, struct

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def read_frame(s, expected_len, timeout=0.5):
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < expected_len and time.monotonic() < deadline:
        chunk = s.read(expected_len - len(buf))
        if chunk:
            buf.extend(chunk)
    return bytes(buf)

def resync(s):
    """Drain the bus until silence — gets us back to a frame boundary."""
    while True:
        chunk = s.read(64)
        if not chunk:
            break

s = serial.Serial('/dev/ttyUSB0', 9600, parity='N', stopbits=1, bytesize=8, timeout=0.02)

print("Sniffing Modbus RTU...")
while True:
    req = read_frame(s, 8, timeout=1.0)
    if len(req) < 8:
        continue

    # Validate before trusting the count field
    if crc16(req) != 0:
        resync(s)
        continue

    slave, fc = req[0], req[1]
    addr  = (req[2] << 8) | req[3]
    count = (req[4] << 8) | req[5]

    # Sanity check — real FC03 requests never ask for >125 registers
    if count > 125:
        resync(s)
        continue

    print(f"  REQ  slave={slave} FC03 addr=0x{addr:04x} count={count}")

    resp_len = 3 + count * 2 + 2
    # Timeout: bytes_to_receive * 1.1ms + 100ms margin
    timeout = (resp_len * 0.0011) + 0.1
    resp = read_frame(s, resp_len, timeout=timeout)

    if len(resp) < resp_len:
        print(f"  RESP timeout — {len(resp)}/{resp_len} bytes — resyncing")
        resync(s)
        continue

    if crc16(resp) != 0:
        print(f"  RESP bad CRC — resyncing")
        resync(s)
        continue

    n = resp[2]
    floats = [struct.unpack('>f', resp[3+i:3+i+4])[0] for i in range(0, n, 4)]
    print(f"  RESP slave={slave} → {[f'{v:.3f}' for v in floats]}")
