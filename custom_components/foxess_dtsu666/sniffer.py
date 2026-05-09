"""Async passive Modbus RTU sniffer — never writes to the bus."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .registers import PARSERS, crc16

_LOGGER = logging.getLogger(__name__)

_FC03 = 0x03
_REQ_LEN = 8  # slave + FC + addr_hi + addr_lo + cnt_hi + cnt_lo + CRC_lo + CRC_hi


class SnifferStatus(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


_ERR_WINDOW = 300.0  # seconds for windowed error rate sensors


@dataclass
class SnifferStats:
    crc_err_times: deque = field(default_factory=lambda: deque(maxlen=500))
    timeout_times: deque = field(default_factory=lambda: deque(maxlen=500))
    resync_times: deque = field(default_factory=lambda: deque(maxlen=500))
    addr_timeout: dict = field(default_factory=lambda: defaultdict(int))
    addr_crc_err: dict = field(default_factory=lambda: defaultdict(int))
    addr_poll_times: dict = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=20)))
    addr_last_ok: dict = field(default_factory=dict)
    addr_response_ms: dict = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=20)))
    last_error_at: float | None = None  # wall-clock time.time()
    consecutive_errors: int = 0
    unknown_addrs: dict = field(default_factory=lambda: defaultdict(int))  # addr → valid-response count

    def count_recent(self, times: deque) -> int:
        cutoff = time.monotonic() - _ERR_WINDOW
        return sum(1 for t in times if t >= cutoff)


class _SerialProtocol(asyncio.Protocol):
    """asyncio Protocol that feeds bytes into a queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        # Persistent receive buffer — bytes left over after a read() are kept here
        # so the next read() call sees them first. Without this, any chunk larger
        # than the requested read size silently drops the trailing bytes.
        self._buf = bytearray()
        self._transport: asyncio.Transport | None = None
        self._eof = asyncio.Event()

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport

    def data_received(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def connection_lost(self, exc: Exception | None) -> None:
        self._eof.set()

    async def read(self, n: int, deadline: float) -> bytes:
        """Read exactly n bytes, or fewer if deadline expires."""
        while len(self._buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                self._buf.extend(chunk)
            except asyncio.TimeoutError:
                break
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    async def drain_until_quiet(self, max_wait: float = 2.0) -> None:
        """Discard buffered bytes until 50 ms of bus silence or max_wait seconds."""
        self._buf.clear()
        deadline = time.monotonic() + max_wait
        while True:
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(self._queue.get(), timeout=min(0.05, max(remaining, 0)))
            except asyncio.TimeoutError:
                return

    def close(self) -> None:
        if self._transport:
            self._transport.close()


class _TcpProtocol(_SerialProtocol):
    """Same as _SerialProtocol; asyncio.open_connection already handles framing."""


class ModbusRtuSniffer:
    """Passive Modbus RTU sniffer. Supports serial and TCP transports."""

    def __init__(
        self,
        *,
        on_response: Callable[[int, int, dict], None],
        on_status: Callable[[SnifferStatus], None],
        # serial params
        serial_port: str | None = None,
        baudrate: int = 9600,
        parity: str = "N",
        bytesize: int = 8,
        stopbits: int = 1,
        # tcp params
        host: str | None = None,
        port: int = 23,
        # active polling fallback
        active_poll_trigger: float = 15.0,
        initial_slave_id: int | None = None,
        always_poll: bool = False,
    ) -> None:
        if serial_port is None and host is None:
            raise ValueError("Either serial_port or host must be provided")
        self._on_response = on_response
        self._on_status = on_status
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._parity = parity
        self._bytesize = bytesize
        self._stopbits = stopbits
        self._host = host
        self._port = port
        self._active_poll_trigger = active_poll_trigger
        self.stats = SnifferStats()
        self._protocol: _SerialProtocol | None = None
        self._write_fn: Optional[Callable[[bytes], None]] = None
        self._active_slave: int | None = initial_slave_id
        self._always_poll: bool = always_poll
        self._active_mode: bool = False
        self._stopped = False
        self._logged_unknown: set[int] = set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            try:
                await self._connect()
                backoff = 1.0
                self._on_status(SnifferStatus.CONNECTED)
                _LOGGER.info("DTSU666 sniffer connected")
                await self._loop()
            except (OSError, asyncio.IncompleteReadError) as exc:
                if self._stopped:
                    return
                _LOGGER.warning("DTSU666 sniffer connection lost: %s — retrying in %.0fs", exc, backoff)
            except asyncio.CancelledError:
                return
            finally:
                self._close()
                if not self._stopped:
                    self._on_status(SnifferStatus.RECONNECTING)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._stopped = True
        self._close()

    def _close(self) -> None:
        if self._protocol:
            self._protocol.close()
            self._protocol = None
        self._write_fn = None
        self._active_mode = False
        self._on_status(SnifferStatus.DISCONNECTED)

    async def _connect(self) -> None:
        if self._serial_port:
            import serial_asyncio_fast  # pyserial-asyncio-fast

            _LOGGER.info(
                "Opening serial port %s baud=%d parity=%s bytesize=%d stopbits=%d",
                self._serial_port, self._baudrate, self._parity, self._bytesize, self._stopbits,
            )
            loop = asyncio.get_running_loop()
            transport, protocol = await serial_asyncio_fast.create_serial_connection(
                loop,
                _SerialProtocol,
                self._serial_port,
                baudrate=self._baudrate,
                parity=self._parity,
                bytesize=self._bytesize,
                stopbits=self._stopbits,
            )
            self._protocol = protocol
            self._write_fn = transport.write
        else:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            proto = _TcpProtocol()
            proto.connection_made(writer.transport)
            self._write_fn = writer.write

            async def _tcp_feed() -> None:
                try:
                    while True:
                        data = await reader.read(256)
                        if not data:
                            proto.connection_lost(None)
                            return
                        proto.data_received(data)
                except Exception as exc:
                    proto.connection_lost(exc)

            asyncio.ensure_future(_tcp_feed())
            self._protocol = proto

    async def _loop(self) -> None:
        proto = self._protocol
        # TCP idle watchdog: if no bytes for 2× max expected frame time, reconnect.
        # Worst case: 125 registers × 2 B × 1.1 ms/B + 100 ms margin ≈ 375 ms × 2
        _watchdog = 1.0

        # Discard whatever mid-frame bytes are already on the bus so the first
        # read below always starts at the beginning of a fresh request.
        await proto.drain_until_quiet()

        # With always_poll=True, start at epoch so the trigger fires immediately.
        last_valid = 0.0 if self._always_poll else time.monotonic()

        while not self._stopped:
            deadline = time.monotonic() + 1.0
            req = await proto.read(_REQ_LEN, deadline)

            if len(req) < _REQ_LEN:
                # Bus was silent — normal during inverter idle.
                if (self._active_slave is not None
                        and self._write_fn is not None
                        and time.monotonic() - last_valid > self._active_poll_trigger):
                    if not self._active_mode:
                        self._active_mode = True
                        _LOGGER.info(
                            "DTSU666 inverter silent for >%gs, switching to active polling (slave=0x%02x)",
                            self._active_poll_trigger, self._active_slave,
                        )
                    await self._active_poll_once(proto)
                continue

            if crc16(req) != 0:
                self.stats.crc_err_times.append(time.monotonic())
                self.stats.resync_times.append(time.monotonic())
                _LOGGER.debug("Bad request CRC, resyncing")
                await proto.drain_until_quiet()
                continue

            slave = req[0]
            fc = req[1]
            addr = (req[2] << 8) | req[3]
            count = (req[4] << 8) | req[5]

            if fc != _FC03 or count > 125 or count == 0:
                await proto.drain_until_quiet()
                continue

            t0 = time.monotonic()

            resp_len = 3 + count * 2 + 2
            timeout = (resp_len * 0.0011) + 0.1
            deadline = time.monotonic() + timeout
            resp = await proto.read(resp_len, deadline)

            if len(resp) < resp_len:
                self.stats.timeout_times.append(time.monotonic())
                self.stats.addr_timeout[addr] += 1
                self.stats.last_error_at = time.time()
                self.stats.consecutive_errors += 1
                _LOGGER.debug("Response timeout for addr=0x%04x (%d/%d bytes)", addr, len(resp), resp_len)
                await proto.drain_until_quiet()
                continue

            if crc16(resp) != 0:
                self.stats.crc_err_times.append(time.monotonic())
                self.stats.addr_crc_err[addr] += 1
                self.stats.last_error_at = time.time()
                self.stats.consecutive_errors += 1
                _LOGGER.debug("Bad response CRC for addr=0x%04x", addr)
                await proto.drain_until_quiet()
                continue

            # Success
            last_valid = time.monotonic()
            self._active_slave = slave
            self.stats.consecutive_errors = 0
            if self._active_mode:
                self._active_mode = False
                _LOGGER.info("DTSU666 inverter resumed polling, switching back to passive")

            elapsed = time.monotonic() - t0
            self.stats.addr_response_ms[addr].append(round(elapsed * 1000, 1))
            prev = self.stats.addr_last_ok.get(addr)
            now = time.monotonic()
            if prev is not None:
                self.stats.addr_poll_times[addr].append(now - prev)
            self.stats.addr_last_ok[addr] = now

            if addr in PARSERS:
                byte_count = resp[2]
                _, parser, _ = PARSERS[addr]
                try:
                    parsed = parser(resp[3 : 3 + byte_count])
                    _LOGGER.debug("slave=0x%02x addr=0x%04x decoded %d fields in %.1fms", slave, addr, len(parsed), elapsed * 1000)
                    self._on_response(slave, addr, parsed)
                except Exception:
                    _LOGGER.exception("Parser error for addr=0x%04x", addr)
            else:
                self.stats.unknown_addrs[addr] += 1
                if addr not in self._logged_unknown:
                    self._logged_unknown.add(addr)
                    byte_count = resp[2]
                    payload_hex = resp[3 : 3 + byte_count].hex(" ")
                    _LOGGER.info(
                        "DTSU666 slave=0x%02x: unknown endpoint 0x%04x (valid CRC, %d regs) — not in PARSERS; payload: %s",
                        slave, addr, count, payload_hex,
                    )

    @staticmethod
    def _build_fc03(slave: int, addr: int, count: int) -> bytes:

        req = bytes([slave, 0x03, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF])
        crc = crc16(req)
        return req + bytes([crc & 0xFF, crc >> 8])

    async def _active_poll_once(self, proto: _SerialProtocol) -> None:
        """Poll all register groups directly as bus master (inverter is offline)."""
        slave = self._active_slave
        for addr, (count, parser, _) in PARSERS.items():
            if self._stopped:
                return

            req = self._build_fc03(slave, addr, count)
            try:
                self._write_fn(req)
            except Exception as exc:
                _LOGGER.debug("Active poll: write error: %s", exc)
                return

            resp_len = 3 + count * 2 + 2
            deadline = time.monotonic() + resp_len * 0.0011 + 0.15
            resp = await proto.read(resp_len, deadline)

            if len(resp) < resp_len:
                _LOGGER.debug("Active poll: timeout for addr=0x%04x", addr)
                await proto.drain_until_quiet()
                continue

            if crc16(resp) != 0:
                _LOGGER.debug("Active poll: bad CRC for addr=0x%04x", addr)
                await proto.drain_until_quiet()
                continue

            byte_count = resp[2]
            try:
                parsed = parser(resp[3:3 + byte_count])
                _LOGGER.debug("Active poll: slave=0x%02x addr=0x%04x OK", slave, addr)
                self._on_response(slave, addr, parsed)
            except Exception:
                _LOGGER.exception("Active poll: parser error for addr=0x%04x", addr)


async def async_discover_slaves(
    *,
    serial_port: str | None = None,
    baudrate: int = 9600,
    parity: str = "N",
    bytesize: int = 8,
    stopbits: int = 1,
    host: str | None = None,
    port: int = 23,
    timeout: float = 10.0,
) -> list[int]:
    """Open the bus briefly, passively sniff for Modbus RTU frames, return unique slave IDs."""
    protocol: _SerialProtocol | None = None
    feed_task = None
    try:
        if serial_port:
            import serial_asyncio_fast
            loop = asyncio.get_running_loop()
            _, protocol = await serial_asyncio_fast.create_serial_connection(
                loop, _SerialProtocol, serial_port,
                baudrate=baudrate, parity=parity, bytesize=bytesize, stopbits=stopbits,
            )
        else:
            reader, writer = await asyncio.open_connection(host, port)
            protocol = _TcpProtocol()
            protocol.connection_made(writer.transport)

            async def _feed() -> None:
                try:
                    while True:
                        data = await reader.read(256)
                        if not data:
                            return
                        protocol.data_received(data)
                except Exception:
                    pass

            feed_task = asyncio.ensure_future(_feed())

        discovered: set[int] = set()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            req = await protocol.read(_REQ_LEN, time.monotonic() + min(1.0, remaining))
            if len(req) < _REQ_LEN:
                continue
            if crc16(req) != 0:
                await protocol.drain_until_quiet(max_wait=min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            slave, fc = req[0], req[1]
            count = (req[4] << 8) | req[5]
            if fc == _FC03 and 0 < count <= 125:
                discovered.add(slave)
            await protocol.drain_until_quiet(max_wait=min(0.5, max(0.0, deadline - time.monotonic())))

        return sorted(discovered)

    except Exception as exc:
        _LOGGER.debug("Slave discovery failed: %s", exc)
        return []
    finally:
        if feed_task:
            feed_task.cancel()
        if protocol:
            protocol.close()
