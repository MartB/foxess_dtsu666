"""Async passive Modbus RTU sniffer — never writes to the bus (except optional active poll)."""
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
_FC03_ERR = _FC03 | 0x80
_REQ_LEN = 8  # slave + FC + addr_hi + addr_lo + cnt_hi + cnt_lo + CRC_lo + CRC_hi
_EXC_LEN = 5  # slave + FC|0x80 + exc_code + CRC_lo + CRC_hi


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
    addr_exception: dict = field(default_factory=lambda: defaultdict(int))  # addr → Modbus exception count
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
        # Persistent receive buffer — bytes left over after a read()/read_frame()
        # are kept here so the next call sees them first.
        self._buf = bytearray()
        self._transport: asyncio.Transport | None = None
        self._eof = asyncio.Event()
        # If the transport reports an exception via connection_lost(exc), it is
        # re-raised from read*/drain_until_quiet so the outer reconnect loop
        # in ModbusRtuSniffer.run() actually catches it.
        self._exc: BaseException | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport

    def data_received(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def connection_lost(self, exc: Exception | None) -> None:
        self._exc = exc
        self._eof.set()

    def _raise_if_closed(self) -> None:
        """Raise if the connection is closed and all queued data has been drained.

        We only raise once the buffer AND the queue are empty so any bytes
        delivered just before disconnect are still returned to the caller.
        """
        if self._eof.is_set() and self._queue.empty() and not self._buf:
            if self._exc is not None:
                raise self._exc
            raise ConnectionResetError("connection closed by peer")

    async def read(self, n: int, deadline: float) -> bytes:
        """Read exactly n bytes, or fewer if deadline expires.

        Kept for callers that want fixed-length reads. Passive sniffing uses
        read_frame() instead.
        """
        while len(self._buf) < n:
            self._raise_if_closed()
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

    async def read_frame(self, first_timeout: float, gap: float) -> bytes:
        """Read one silence-delimited frame.

        Waits up to `first_timeout` for the first byte; then keeps collecting
        bytes, restarting a `gap`-second idle timer on each arrival. Returns
        the bytes accumulated once `gap` seconds of bus silence elapse. Returns
        b"" if `first_timeout` passes with no data (a quiet bus).

        This is how Modbus RTU actually delimits frames (>= 3.5 char times of
        idle). A returned "frame" may contain more than one Modbus frame if the
        bus turnaround was faster than `gap` — callers split it structurally.
        """
        self._raise_if_closed()
        frame = bytearray()

        # Consume any leftover from a previous fixed-length read first.
        if self._buf:
            frame.extend(self._buf)
            self._buf.clear()

        if not frame:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=first_timeout)
                frame.extend(chunk)
            except asyncio.TimeoutError:
                self._raise_if_closed()
                return b""

        while True:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=gap)
                frame.extend(chunk)
            except asyncio.TimeoutError:
                break
            self._raise_if_closed()

        return bytes(frame)

    async def drain_until_quiet(self, max_wait: float = 2.0, quiet: float = 0.05) -> None:
        """Discard buffered bytes until `quiet` s of bus silence or max_wait s."""
        self._buf.clear()
        deadline = time.monotonic() + max_wait
        while True:
            self._raise_if_closed()
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(self._queue.get(), timeout=min(quiet, max(remaining, 0)))
            except asyncio.TimeoutError:
                return

    def close(self) -> None:
        if self._transport:
            self._transport.close()


class _TcpProtocol(_SerialProtocol):
    """Same as _SerialProtocol; asyncio.open_connection already handles framing."""


def _compute_timing(baudrate: int, bytesize: int, parity: str, stopbits: float) -> tuple[float, float]:
    """Return (char_time, frame_gap) in seconds for Modbus RTU framing.

    frame_gap is the inter-frame silence used to delimit frames: 3.5 char times,
    but a fixed 1.75 ms above 19200 baud per the Modbus spec. It is floored to
    guard against OS / USB-serial scheduling jitter that would otherwise split a
    single frame in two (which is the dangerous failure — merges are handled
    structurally by the parser, splits are not).
    """
    bits_per_char = 1 + bytesize + (0 if parity == "N" else 1) + int(round(stopbits))
    char_time = bits_per_char / float(baudrate)
    frame_gap = 0.00175 if baudrate > 19200 else 3.5 * char_time
    frame_gap = max(frame_gap, 0.005)
    return char_time, frame_gap


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
        self._last_valid: float = 0.0
        # RTU timing derived from the configured line settings (not hardcoded).
        self._char_time, self._frame_gap = _compute_timing(baudrate, bytesize, parity, stopbits)

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

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        proto = self._protocol

        # Discard whatever mid-frame bytes are already on the bus so the first
        # frame we parse starts at a real boundary.
        await proto.drain_until_quiet()

        # With always_poll=True, start at epoch so the trigger fires immediately.
        self._last_valid = 0.0 if self._always_poll else time.monotonic()
        pending: tuple[int, int, int, float] | None = None  # (slave, addr, count, t0)

        while not self._stopped:
            frame = await proto.read_frame(first_timeout=1.0, gap=self._frame_gap)

            if not frame:
                # Bus silent for ~1s.
                if pending is not None:
                    # The request we saw never got a response within the window.
                    self._record_timeout(pending[1])
                    pending = None
                if (self._active_slave is not None
                        and self._write_fn is not None
                        and time.monotonic() - self._last_valid > self._active_poll_trigger):
                    if not self._active_mode:
                        self._active_mode = True
                        _LOGGER.info(
                            "DTSU666 inverter silent for >%gs, switching to active polling (slave=0x%02x)",
                            self._active_poll_trigger, self._active_slave,
                        )
                    await self._active_poll_once(proto)
                continue

            pending = await self._process_frame(frame, pending, proto)

    async def _process_frame(self, frame: bytes, pending, proto: _SerialProtocol):
        """Parse one silence-delimited chunk structurally, peeling Modbus frames
        from the front. Returns the new `pending` request state.

        Disambiguation note: an FC03 *request* is exactly 8 bytes (even); an FC03
        *response* is 5 + byte_count bytes (odd, since byte_count == 2*regs). So
        length + CRC reliably tells them apart, and `pending` resolves the rest.
        """
        i, n = 0, len(frame)
        while i < n:
            remaining = n - i

            if pending is None:
                # Expect a request.
                if (remaining >= _REQ_LEN
                        and frame[i + 1] == _FC03
                        and crc16(frame[i:i + _REQ_LEN]) == 0):
                    count = (frame[i + 4] << 8) | frame[i + 5]
                    if 0 < count <= 125:
                        addr = (frame[i + 2] << 8) | frame[i + 3]
                        pending = (frame[i], addr, count, time.monotonic())
                        i += _REQ_LEN
                        continue
                # Not a clean request at this offset → resync.
                self._note_resync()
                await proto.drain_until_quiet(quiet=max(self._frame_gap * 1.5, 0.05))
                return None

            slave, addr, count, t0 = pending

            # 1) Exception response: slave, FC|0x80, code, CRC (5 bytes).
            if (remaining >= _EXC_LEN
                    and frame[i + 1] == _FC03_ERR
                    and crc16(frame[i:i + _EXC_LEN]) == 0):
                self._record_exception(addr, frame[i + 2])
                pending = None
                i += _EXC_LEN
                continue

            # 2) Normal response: slave, FC, byte_count, data..., CRC.
            if remaining >= 3 and frame[i + 1] == _FC03:
                byte_count = frame[i + 2]
                resp_len = 5 + byte_count
                if (byte_count == count * 2
                        and remaining >= resp_len
                        and crc16(frame[i:i + resp_len]) == 0):
                    self._record_success(slave, addr, count, t0, frame[i:i + resp_len])
                    pending = None
                    i += resp_len
                    continue

            # 3) Not a valid response — maybe the prior request went unanswered
            #    and this is the next request.
            if (remaining >= _REQ_LEN
                    and frame[i + 1] == _FC03
                    and crc16(frame[i:i + _REQ_LEN]) == 0):
                ncount = (frame[i + 4] << 8) | frame[i + 5]
                if 0 < ncount <= 125:
                    self._record_timeout(addr)  # prior request never answered
                    naddr = (frame[i + 2] << 8) | frame[i + 3]
                    pending = (frame[i], naddr, ncount, time.monotonic())
                    i += _REQ_LEN
                    continue

            # 4) Genuine garbage / corrupted response.
            self._record_crc_error(addr)
            await proto.drain_until_quiet(quiet=max(self._frame_gap * 1.5, 0.05))
            return None

        return pending

    # ----------------------------------------------------------- stat helpers

    def _note_resync(self) -> None:
        now = time.monotonic()
        self.stats.crc_err_times.append(now)
        self.stats.resync_times.append(now)
        _LOGGER.debug("Framing resync")

    def _record_timeout(self, addr: int) -> None:
        self.stats.timeout_times.append(time.monotonic())
        self.stats.addr_timeout[addr] += 1
        self.stats.last_error_at = time.time()
        self.stats.consecutive_errors += 1
        _LOGGER.debug("Response timeout for addr=0x%04x", addr)

    def _record_crc_error(self, addr: int) -> None:
        now = time.monotonic()
        self.stats.crc_err_times.append(now)
        self.stats.resync_times.append(now)
        self.stats.addr_crc_err[addr] += 1
        self.stats.last_error_at = time.time()
        self.stats.consecutive_errors += 1
        _LOGGER.debug("Bad response framing/CRC for addr=0x%04x", addr)

    def _record_exception(self, addr: int, code: int) -> None:
        self.stats.addr_exception[addr] += 1
        self.stats.last_error_at = time.time()
        self.stats.consecutive_errors += 1
        _LOGGER.debug("Modbus exception 0x%02x for addr=0x%04x", code, addr)

    def _record_success(self, slave: int, addr: int, count: int, t0: float, frame: bytes) -> None:
        now = time.monotonic()
        self._last_valid = now
        self._active_slave = slave
        self.stats.consecutive_errors = 0
        if self._active_mode:
            self._active_mode = False
            _LOGGER.info("DTSU666 inverter resumed polling, switching back to passive")

        elapsed = now - t0
        self.stats.addr_response_ms[addr].append(round(elapsed * 1000, 1))
        prev = self.stats.addr_last_ok.get(addr)
        if prev is not None:
            self.stats.addr_poll_times[addr].append(now - prev)
        self.stats.addr_last_ok[addr] = now

        byte_count = frame[2]
        payload = frame[3:3 + byte_count]
        if addr in PARSERS:
            _, parser, _ = PARSERS[addr]
            try:
                parsed = parser(payload)
                _LOGGER.debug("slave=0x%02x addr=0x%04x decoded %d fields in %.1fms",
                              slave, addr, len(parsed), elapsed * 1000)
                self._on_response(slave, addr, parsed)
            except Exception:
                _LOGGER.exception("Parser error for addr=0x%04x", addr)
        else:
            self.stats.unknown_addrs[addr] += 1
            if addr not in self._logged_unknown:
                self._logged_unknown.add(addr)
                _LOGGER.info(
                    "DTSU666 slave=0x%02x: unknown endpoint 0x%04x (valid CRC, %d regs) — not in PARSERS; payload: %s",
                    slave, addr, count, payload.hex(" "),
                )

    # ------------------------------------------------------------- active poll

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

            resp_len = 5 + count * 2
            first_to = resp_len * self._char_time + 0.15
            frame = await proto.read_frame(first_timeout=first_to, gap=self._frame_gap)

            if not frame:
                _LOGGER.debug("Active poll: timeout for addr=0x%04x", addr)
                continue

            if (len(frame) >= _EXC_LEN
                    and frame[1] == _FC03_ERR
                    and crc16(frame[:_EXC_LEN]) == 0):
                _LOGGER.debug("Active poll: exception 0x%02x for addr=0x%04x", frame[2], addr)
                continue

            if len(frame) < resp_len or frame[1] != _FC03 or crc16(frame[:resp_len]) != 0:
                _LOGGER.debug("Active poll: bad/short frame for addr=0x%04x", addr)
                await proto.drain_until_quiet(quiet=max(self._frame_gap * 1.5, 0.05))
                continue

            byte_count = frame[2]
            try:
                parsed = parser(frame[3:3 + byte_count])
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
    _, frame_gap = _compute_timing(baudrate, bytesize, parity, stopbits)
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
            frame = await protocol.read_frame(first_timeout=min(1.0, max(0.0, remaining)), gap=frame_gap)
            if not frame:
                continue
            # A chunk can hold several frames; scan for valid 8-byte FC03 requests.
            i, n = 0, len(frame)
            while i + _REQ_LEN <= n:
                if frame[i + 1] == _FC03 and crc16(frame[i:i + _REQ_LEN]) == 0:
                    count = (frame[i + 4] << 8) | frame[i + 5]
                    if 0 < count <= 125:
                        discovered.add(frame[i])
                    i += _REQ_LEN
                else:
                    i += 1  # slide past a response / noise to find the next request

        return sorted(discovered)

    except Exception as exc:
        _LOGGER.debug("Slave discovery failed: %s", exc)
        return []
    finally:
        if feed_task:
            feed_task.cancel()
        if protocol:
            protocol.close()