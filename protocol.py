"""Datacenter transport: 0-RTT, message framing, SACK, delay telemetry.

The design is in README.md and the CHANGED_*.md notes. This is milestone M1:
wire format, framing, receiver reassembly, an ACK for every packet, and an
RTO retransmit. Senders still send at line rate (no pacer yet, M3), and loss
recovery is timer-only (SACK fast retransmit and tail probes are M2).

Sections:
  1. constants
  2. codec            pack/unpack for the data and ACK headers
  3. RangeSet         sorted disjoint byte intervals
  4. FlowTx           per-destination sender state
  5. MySender
  6. MyReceiver
"""

from __future__ import annotations

import struct

from transportlab.api import Receiver, Sender

SCENARIO = "datacenter"

# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------

MAX_DATAGRAM = 1472
DATA_HDR = 16
ACK_HDR = 14
SACK_BLOCK = 8
MAX_PAYLOAD = MAX_DATAGRAM - DATA_HDR   # 1456
MAX_SACK = 4                            # blocks per ACK (field allows 15)
RWND = 0xFFFF                           # 256-byte units; never binding here

# Flags live in the high nibble of byte 0; #SACK in the low nibble.
F_ACK = 0x8
F_END = 0x4
F_PROBE = 0x2

# Retransmit timer. Sim RTT is 80-100 ms; real units are 100x smaller.
RTO_INIT_S = 0.25
RTO_MIN_S = 0.12
RTO_MAX_S = 2.0

_U32 = 1 << 32
_U24_MAX = (1 << 24) - 1


def now_us(t: float) -> int:
    return int(t * 1_000_000) % _U32


# ---------------------------------------------------------------------------
# 2. Codec
# ---------------------------------------------------------------------------

# Data: flags|#sack (1), seq (4), send_ts (4), msg_id (4), msg_offset (3)
_DATA = struct.Struct("!BIII")
# ACK: flags|#sack (1), cum_ack (4), rwnd (2), echo_ts (4), owd (3)
_ACK = struct.Struct("!BIHI")
_SACK = struct.Struct("!II")


def pack_data(seq, ts, msg_id, offset, payload, end=False, probe=False) -> bytes:
    flags = (F_END if end else 0) | (F_PROBE if probe else 0)
    return (_DATA.pack(flags << 4, seq % _U32, ts, msg_id)
            + offset.to_bytes(3, "big") + payload)


def unpack_data(buf: bytes):
    """-> (flags, seq, ts, msg_id, offset, payload)."""
    b0, seq, ts, msg_id = _DATA.unpack_from(buf)
    offset = int.from_bytes(buf[13:16], "big")
    return b0 >> 4, seq, ts, msg_id, offset, buf[DATA_HDR:]


def pack_ack(cum, echo_ts, owd_us, sacks=(), rwnd=RWND) -> bytes:
    sacks = list(sacks)[:15]
    out = _ACK.pack((F_ACK << 4) | len(sacks), cum % _U32, rwnd, echo_ts)
    out += max(0, min(owd_us, _U24_MAX)).to_bytes(3, "big")
    for left, right in sacks:
        out += _SACK.pack(left % _U32, right % _U32)
    return out


def unpack_ack(buf: bytes):
    """-> (cum, rwnd, echo_ts, owd_us, [(left, right), ...])."""
    b0, cum, rwnd, echo = _ACK.unpack_from(buf)
    owd = int.from_bytes(buf[11:14], "big")
    n = b0 & 0xF
    sacks = [_SACK.unpack_from(buf, ACK_HDR + i * SACK_BLOCK) for i in range(n)
             if ACK_HDR + (i + 1) * SACK_BLOCK <= len(buf)]
    return cum, rwnd, echo, owd, sacks


def is_ack(buf: bytes) -> bool:
    return len(buf) >= 1 and (buf[0] >> 4) & F_ACK != 0


# ---------------------------------------------------------------------------
# 3. RangeSet
# ---------------------------------------------------------------------------

class RangeSet:
    """Sorted, disjoint, non-adjacent half-open intervals [lo, hi)."""

    __slots__ = ("r",)

    def __init__(self):
        self.r: list[list[int]] = []

    def add(self, lo: int, hi: int) -> bool:
        """Add [lo, hi). Returns True if any byte was new."""
        if hi <= lo or self.contains(lo, hi):
            return False
        r = self.r
        i = 0
        while i < len(r) and r[i][1] < lo:   # entirely left of lo, not touching
            i += 1
        j = i
        while j < len(r) and r[j][0] <= hi:  # overlaps or touches
            lo, hi = min(lo, r[j][0]), max(hi, r[j][1])
            j += 1
        r[i:j] = [[lo, hi]]
        return True

    def contains(self, lo: int, hi: int) -> bool:
        for a, b in self.r:
            if a <= lo:
                if hi <= b:
                    return True
            else:
                break
        return False

    def first_end(self, start: int = 0) -> int:
        """End of the contiguous run beginning at `start` (start if none)."""
        if self.r and self.r[0][0] <= start:
            return max(start, self.r[0][1])
        return start

    def above(self, x: int) -> list[tuple[int, int]]:
        return [(max(a, x), b) for a, b in self.r if b > x]

    def covers(self, lo: int, hi: int) -> bool:
        return self.contains(lo, hi)

    def __len__(self):
        return len(self.r)


# ---------------------------------------------------------------------------
# 4. Sender per-flow state
# ---------------------------------------------------------------------------

class Seg:
    __slots__ = ("seq", "msg_id", "offset", "payload", "end", "sent_at", "tx")

    def __init__(self, seq, msg_id, offset, payload, end):
        self.seq = seq
        self.msg_id = msg_id
        self.offset = offset
        self.payload = payload
        self.end = end
        self.sent_at = 0.0
        self.tx = 0

    @property
    def hi(self):
        return self.seq + len(self.payload)


class FlowTx:
    def __init__(self, dst: str):
        self.dst = dst
        self.next_seq = 0
        self.queue: list[Seg] = []          # packetized, not yet sent
        self.unacked: dict[int, Seg] = {}   # seq -> segment, in send order
        self.cum = 0
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.backoff = 0
        self.base_owd_us: int | None = None

    def packetize(self, msg):
        data = msg.payload
        n = len(data)
        off = 0
        while True:
            chunk = data[off:off + MAX_PAYLOAD]
            end = off + len(chunk) >= n
            self.queue.append(Seg(self.next_seq, msg.msg_id, off, chunk, end))
            self.next_seq += len(chunk)
            off += len(chunk)
            if end:
                break

    def rto(self) -> float:
        base = RTO_INIT_S if self.srtt is None else max(
            RTO_MIN_S, self.srtt + 4 * self.rttvar, 1.25 * self.srtt)
        return min(RTO_MAX_S, base * (2 ** self.backoff))

    def on_rtt(self, rtt: float):
        if self.srtt is None:
            self.srtt, self.rttvar = rtt, rtt / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt)
            self.srtt = 0.875 * self.srtt + 0.125 * rtt

    def on_ack(self, cum: int, sacks) -> int:
        """Drop acknowledged segments. Returns bytes newly acknowledged."""
        if cum > self.cum:
            self.cum = cum
        acked = 0
        for seq in list(self.unacked):
            s = self.unacked[seq]
            if s.hi <= self.cum or any(l <= s.seq and s.hi <= r for l, r in sacks):
                acked += len(s.payload)
                del self.unacked[seq]
        return acked


# ---------------------------------------------------------------------------
# 5. Sender
# ---------------------------------------------------------------------------

class MySender(Sender):
    def on_start(self):
        self.flows: dict[str, FlowTx] = {}
        self.addr_to_name = {_norm(a): n for n, a in self.peers.items()}

    def _flow(self, dst: str) -> FlowTx:
        f = self.flows.get(dst)
        if f is None:
            f = self.flows[dst] = FlowTx(dst)
        return f

    def on_app_data(self, msg):
        f = self._flow(msg.dst)
        f.packetize(msg)
        # Gather the whole round before sending (every message of a round is
        # offered at the same instant).
        if not self.timer_pending("flush"):
            self.set_timer("flush", 0.0)

    def _send_seg(self, f: FlowTx, s: Seg, probe=False):
        t = self.now()
        s.sent_at = t
        s.tx += 1
        self.send_datagram(
            pack_data(s.seq, now_us(t), s.msg_id, s.offset, s.payload, s.end, probe),
            to=f.dst)

    def _flush(self):
        # Round-robin across flows so no destination gets a head start.
        active = [f for f in self.flows.values() if f.queue]
        while active:
            nxt = []
            for f in active:
                s = f.queue.pop(0)
                f.unacked[s.seq] = s
                self._send_seg(f, s)
                if f.queue:
                    nxt.append(f)
            active = nxt
        for f in self.flows.values():
            if f.unacked and not self.timer_pending("rto:" + f.dst):
                self.set_timer("rto:" + f.dst, f.rto())

    def on_datagram(self, data, addr):
        if len(data) < ACK_HDR or not is_ack(data):
            return
        name = self.addr_to_name.get(_norm(addr))
        f = self.flows.get(name) if name else None
        if f is None:
            return
        cum, _rwnd, echo, owd, sacks = unpack_ack(data)
        rtt = ((now_us(self.now()) - echo) % _U32) / 1e6
        if 0 < rtt < 10:
            f.on_rtt(rtt)
        if f.base_owd_us is None or owd < f.base_owd_us:
            f.base_owd_us = owd
        if f.on_ack(cum, sacks):
            f.backoff = 0
            if f.unacked:
                self.set_timer("rto:" + f.dst, f.rto())
        if not f.unacked:
            self.cancel_timer("rto:" + f.dst)

    def on_timer(self, name):
        if name == "flush":
            self._flush()
        elif name.startswith("rto:"):
            f = self.flows.get(name[4:])
            if f is None or not f.unacked:
                return
            # Timer-only recovery for M1: resend everything still unacked.
            for s in list(f.unacked.values()):
                self._send_seg(f, s, probe=True)
            f.backoff = min(f.backoff + 1, 5)
            self.set_timer(name, f.rto())

    def on_stop(self):
        pass


# ---------------------------------------------------------------------------
# 6. Receiver
# ---------------------------------------------------------------------------

class _Msg:
    __slots__ = ("chunks", "got", "total")

    def __init__(self):
        self.chunks: dict[int, bytes] = {}
        self.got = RangeSet()
        self.total: int | None = None


class FlowRx:
    def __init__(self):
        self.stream = RangeSet()
        self.msgs: dict[int, _Msg] = {}
        self.done: set[int] = set()


class MyReceiver(Receiver):
    def on_start(self):
        self.flows: dict[tuple, FlowRx] = {}

    def on_datagram(self, data, addr):
        if len(data) < DATA_HDR or is_ack(data):
            return
        _flags, seq, ts, msg_id, offset, payload = unpack_data(data)
        end = bool(_flags & F_END)
        key = _norm(addr)
        f = self.flows.get(key)
        if f is None:
            f = self.flows[key] = FlowRx()
        f.stream.add(seq, seq + len(payload))

        if msg_id not in f.done and not self.delivered(msg_id):
            m = f.msgs.get(msg_id)
            if m is None:
                m = f.msgs[msg_id] = _Msg()
            if m.got.add(offset, offset + len(payload)) or (end and not payload):
                m.chunks[offset] = payload
            if end:
                m.total = offset + len(payload)
            if m.total is not None and (m.total == 0 or m.got.covers(0, m.total)):
                buf = bytearray(m.total)
                for off, chunk in m.chunks.items():
                    buf[off:off + len(chunk)] = chunk
                self.deliver(msg_id, bytes(buf))
                f.done.add(msg_id)
                del f.msgs[msg_id]

        # Always ACK, duplicates included, or a lost ACK wedges the sender.
        cum = f.stream.first_end(0)
        blocks = f.stream.above(cum)
        # The block holding this packet first (newest), then highest first.
        blocks.sort(key=lambda b: (not (b[0] <= seq < b[1]), -b[0]))
        owd = (now_us(self.now()) - ts) % _U32
        self.send_datagram(pack_ack(cum, ts, owd, blocks[:MAX_SACK]), to=addr)


def _norm(addr) -> tuple:
    return (str(addr[0]), int(addr[1]))


SENDER, RECEIVER = MySender, MyReceiver
