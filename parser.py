import serial, time, struct, collections

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
    while True:
        chunk = s.read(64)
        if not chunk:
            break

def floats(data, offset, count):
    return [struct.unpack('>f', data[offset+i*4:offset+i*4+4])[0] for i in range(count)]

def parse_0x151e(payload):
    f = floats(payload, 0, 7)
    return {
        'power_A_kW':          f[0],
        'power_B_kW':          f[1],
        'power_C_kW':          f[2],
        'reactive_total_kVAr': f[3],
        'reactive_A_kVAr':     f[4],
        'reactive_B_kVAr':     f[5],
        'reactive_C_kVAr':     f[6],
    }

def parse_0x1510(payload):
    f = floats(payload, 0, 32)
    return {
        'voltage_A':              f[0],
        'voltage_B':              f[1],
        'voltage_C':              f[2],
        'current_A':              f[3],
        'current_B':              f[4],
        'current_C':              f[5],
        'power_total_kW':         f[6],
        'power_A_kW':             f[7],
        'power_B_kW':             f[8],
        'power_C_kW':             f[9],
        'reactive_total_kVAr':    f[10],
        'reactive_A_kVAr':        f[11],
        'reactive_B_kVAr':        f[12],
        'reactive_C_kVAr':        f[13],
        'apparent_total_kVA':     f[14],
        'apparent_A_kVA':         f[15],
        'apparent_B_kVA':         f[16],
        'apparent_C_kVA':         f[17],
        'pf_total':               f[18],
        'pf_A':                   f[19],
        'pf_B':                   f[20],
        'pf_C':                   f[21],
        'frequency_Hz':           f[31],
    }

def parse_0x181e(payload):
    f = floats(payload, 0, 10)
    return {
        'energy_import_total_kWh':    f[0],
        'energy_import_A_kWh':        f[1],
        'energy_import_B_kWh':        f[2],
        'energy_import_C_kWh':        f[3],
        'energy_reactive_Q1_kVArh':   f[4],
        'energy_export_total_kWh':    f[5],
        'energy_export_A_kWh':        f[6],
        'energy_export_B_kWh':        f[7],
        'energy_export_C_kWh':        f[8],
        'energy_reactive_total_kVArh':f[9],
    }

PARSERS = {
    0x151e: (14, parse_0x151e),
    0x1510: (64, parse_0x1510),
    0x181e: (20, parse_0x181e),
}

SECTIONS = [
    ('VOLTAGE', [
        ('voltage_A',                  'V',    '0x1510'),
        ('voltage_B',                  'V',    '0x1510'),
        ('voltage_C',                  'V',    '0x1510'),
        ('frequency_Hz',               'Hz',   '0x1510'),
    ]),
    ('CURRENT', [
        ('current_A',                  'A',    '0x1510'),
        ('current_B',                  'A',    '0x1510'),
        ('current_C',                  'A',    '0x1510'),
    ]),
    ('ACTIVE POWER', [
        ('power_total_kW',             'kW',   '0x1510'),
        ('power_A_kW',                 'kW',   '0x151e'),
        ('power_B_kW',                 'kW',   '0x151e'),
        ('power_C_kW',                 'kW',   '0x151e'),
    ]),
    ('REACTIVE POWER', [
        ('reactive_total_kVAr',        'kVAr', '0x151e'),
        ('reactive_A_kVAr',            'kVAr', '0x151e'),
        ('reactive_B_kVAr',            'kVAr', '0x151e'),
        ('reactive_C_kVAr',            'kVAr', '0x151e'),
    ]),
    ('APPARENT POWER', [
        ('apparent_total_kVA',         'kVA',  '0x1510'),
        ('apparent_A_kVA',             'kVA',  '0x1510'),
        ('apparent_B_kVA',             'kVA',  '0x1510'),
        ('apparent_C_kVA',             'kVA',  '0x1510'),
    ]),
    ('POWER FACTOR', [
        ('pf_total',                   '',     '0x1510'),
        ('pf_A',                       '',     '0x1510'),
        ('pf_B',                       '',     '0x1510'),
        ('pf_C',                       '',     '0x1510'),
    ]),
    ('ENERGY', [
        ('energy_import_total_kWh',    'kWh',  '0x181e'),
        ('energy_import_A_kWh',        'kWh',  '0x181e'),
        ('energy_import_B_kWh',        'kWh',  '0x181e'),
        ('energy_import_C_kWh',        'kWh',  '0x181e'),
        ('energy_export_total_kWh',    'kWh',  '0x181e'),
        ('energy_export_A_kWh',        'kWh',  '0x181e'),
        ('energy_export_B_kWh',        'kWh',  '0x181e'),
        ('energy_export_C_kWh',        'kWh',  '0x181e'),
        ('energy_reactive_Q1_kVArh',   'kVArh','0x181e'),
        ('energy_reactive_total_kVArh','kVArh','0x181e'),
    ]),
]

# ── stats ──────────────────────────────────────────────────────────────────────

stats = {
    'total':        0,
    'ok':           0,
    'crc_err':      0,
    'timeout':      0,
    'resync':       0,
    'start':        time.monotonic(),
}

# per-address counters
addr_stats = collections.defaultdict(lambda: {'ok': 0, 'timeout': 0, 'crc_err': 0, 'last_ms': None})

# rolling poll interval (last 20 successful polls per address)
poll_times = collections.defaultdict(lambda: collections.deque(maxlen=20))
last_ok_time = {}

# fault log (last 20 faults)
fault_log = collections.deque(maxlen=20)

def log_fault(kind, addr, detail=''):
    ts = time.strftime('%H:%M:%S')
    fault_log.append(f"{ts}  {kind:<12}  addr=0x{addr:04x}  {detail}")

store = {}

def render():
    now = time.monotonic()
    uptime = int(now - stats['start'])
    total  = stats['total'] or 1
    ok_pct = 100 * stats['ok'] / total

    print('\033[H', end='')
    print(f"  DTSU666 Live  —  {time.strftime('%H:%M:%S')}  "
          f"up {uptime//3600:02d}:{(uptime%3600)//60:02d}:{uptime%60:02d}  "
          f"(Ctrl+C to quit)")
    print(f"  {'─'*62}")

    # ── data sections ──────────────────────────────────────────
    for section_name, fields in SECTIONS:
        print(f"\n  \033[1m{section_name}\033[0m")
        for key, unit, _ in fields:
            val = store.get(key)
            display = f'{val:>10.3f}' if val is not None else '       ---'
            label = key.replace('_', ' ')
            print(f"    {label:<32} {display}  {unit}")

    # ── stats panel ────────────────────────────────────────────
    print(f"\n  \033[1mSTATISTICS\033[0m")
    print(f"    {'Total polls':<32} {stats['total']:>10}")
    print(f"    {'Successful':<32} {stats['ok']:>10}  ({ok_pct:.1f}%)")
    print(f"    {'CRC errors':<32} {stats['crc_err']:>10}")
    print(f"    {'Timeouts':<32} {stats['timeout']:>10}")
    print(f"    {'Resyncs':<32} {stats['resync']:>10}")

    print(f"\n  \033[1mPER-ADDRESS\033[0m")
    print(f"    {'Address':<12} {'OK':>6} {'Timeout':>8} {'CRC err':>8} {'Poll ms':>8}")
    print(f"    {'─'*46}")
    for addr, ac in sorted(addr_stats.items()):
        pts = poll_times[addr]
        avg_ms = (sum(pts) / len(pts) * 1000) if pts else 0
        print(f"    0x{addr:04x}      {ac['ok']:>6} {ac['timeout']:>8} {ac['crc_err']:>8} {avg_ms:>7.1f}ms")

    # ── fault log ──────────────────────────────────────────────
    print(f"\n  \033[1mFAULT LOG\033[0m  (last {fault_log.maxlen})")
    if fault_log:
        for entry in fault_log:
            print(f"    {entry}")
    else:
        print(f"    no faults")

    # pad to clear any leftover lines from previous render
    print('\033[J', end='')

# ── main loop ──────────────────────────────────────────────────────────────────

print('\033[2J', end='')

s = serial.Serial('/dev/ttyUSB0', 9600, parity='N', stopbits=1, bytesize=8, timeout=0.02)

while True:
    t0 = time.monotonic()

    req = read_frame(s, 8, timeout=1.0)
    if len(req) < 8 or crc16(req) != 0:
        stats['crc_err'] += 1
        stats['resync']  += 1
        stats['total']   += 1
        log_fault('BAD_REQ', 0, f'len={len(req)}')
        resync(s)
        continue

    slave = req[0]
    fc    = req[1]
    addr  = (req[2] << 8) | req[3]
    count = (req[4] << 8) | req[5]

    if fc != 3 or count > 125 or slave != 1:
        stats['resync'] += 1
        resync(s)
        continue

    stats['total'] += 1

    resp_len = 3 + count * 2 + 2
    resp = read_frame(s, resp_len, timeout=(resp_len * 0.0011) + 0.1)

    if len(resp) < resp_len:
        stats['timeout']           += 1
        addr_stats[addr]['timeout']+= 1
        log_fault('TIMEOUT', addr, f'got {len(resp)}/{resp_len} bytes')
        resync(s)
        continue

    if crc16(resp) != 0:
        stats['crc_err']           += 1
        addr_stats[addr]['crc_err']+= 1
        log_fault('BAD_CRC', addr)
        resync(s)
        continue

    # success
    elapsed = time.monotonic() - t0
    stats['ok']              += 1
    addr_stats[addr]['ok']   += 1
    if addr in last_ok_time:
        poll_times[addr].append(time.monotonic() - last_ok_time[addr])
    last_ok_time[addr] = time.monotonic()

    if addr in PARSERS:
        _, parser = PARSERS[addr]
        store.update(parser(resp[3:3 + resp[2]]))
        render()
