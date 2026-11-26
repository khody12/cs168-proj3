"""Datacenter transport: 0-RTT, message framing, SACK, delay telemetry.

The design is in README.md and the CHANGED_*.md notes. Done so far: wire
format, framing, receiver reassembly, an ACK for every packet, SACK/RACK loss
detection with tail-loss probes and backoff, and a pacer that starts every
flow at its topology fair share (CHANGED_SLOW_START_DESIGN.md).
Not yet: delay-feedback rate control (M4).

Sections:
  1. constants        incl. the topology the starting rates come from
  2. codec            pack/unpack for the data and ACK headers
  3. RangeSet         sorted disjoint byte intervals
  4. FlowTx           per-destination sender state
  5. MySender         flush -> set rates -> pacer
  6. MyReceiver
"""

from __future__ import annotations

import struct
from collections import deque

from transportlab.api import Receiver, Sender

SCENARIO = "datacenter"

# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------

MAX_DATAGRAM = 1472
DATA_HDR = 9                            # flags|#sack, seq, send_ts
FRAME_HDR = 9                           # msg_id, offset, len|END
ACK_HDR = 14
SACK_BLOCK = 8
MAX_PAYLOAD = MAX_DATAGRAM - DATA_HDR - FRAME_HDR   # 1454: one frame's data
MIN_FRAME_DATA = 64        # don't start a frame in less room than this
MAX_SACK = 4                            # blocks per ACK (field allows 15)
RWND = 0xFFFF                           # 256-byte units; never binding here

# Flags live in the high nibble of byte 0; #SACK in the low nibble.
F_ACK = 0x8
F_PROBE = 0x2
# A frame's END bit (last piece of its message) is the top bit of its length.
FRAME_END = 0x8000

# Loss recovery. Sim RTT is 80-100 ms; real units are 100x smaller.
RTO_INIT_S = 0.15          # loss delay before any RTT sample
PTO_INIT_S = 0.15          # probe timeout before any RTT sample (~1.5 RTT)
PTO_GRANULARITY_S = 0.002
PTO_MAX_RTTS = 4           # backoff cap, in RTTs
TARGET_DELAY_S = 0.0025    # target queueing delay (~30 pkts at 150 Mbps; queues hold 64)
# Loss detection, RACK-style (RFC 8985): a segment is lost once a segment
# sent after it has been acked and it is older than latest_rtt + reo_wnd.
# A flow's packets share one FIFO path, so without reordering a later
# packet's ACK proves an earlier one is lost: reo_wnd starts at 0 and the
# retransmission goes out as soon as that ACK arrives. Once reordering is seen
# (path jitter), reo_wnd widens to min_rtt / 4, capped at srtt, as in RACK.
# (Packet-count thresholds are off: jitter reordering made them fire falsely.)
PKT_THRESH = 0
REO_WND_RTT_FRAC = 0.25
LOSS_TIME_MIN_S = 0.001    # slack for ACK spacing and timer granularity
SPURIOUS_RTT_FRAC = 0.8    # a retransmission "acked" faster than this * min_rtt
                           # must have been acked by the original: reordering
TIMER_FLOOR_S = 0.0001

# Topology, from scenarios/datacenter/scenario.yaml. The protocol isn't told
# any of this at runtime; it stands in for the hop records' link-bandwidth
# field (CHANGED_SLOW_START_DESIGN.md).
RACK_SIZE = 3              # servers per rack; server N is in rack N // 3
NIC_BPS = 100e6            # server <-> rack switch links
SPINE_BPS = 150e6          # rack switch <-> spine links
WIRE_OVERHEAD = 28         # IPv4 + UDP bytes the harness charges per datagram
# One sender's share of its rack's uplink to the spine, assuming all servers
# in the rack are sending to other racks (all-to-all): 150 / 3 = 50 Mbps,
# minus room for ACKs. The same rack->spine link also carries the ACKs for
# all data coming *into* the rack (one ~42 B ACK per ~1500 B data packet), so
# filling it 100% with data would overload it by ~3% and slowly overflow its
# queue on long rounds.
ACK_SHARE = (ACK_HDR + WIRE_OVERHEAD) / (MAX_DATAGRAM + WIRE_OVERHEAD)
CROSS_RACK_BPS = SPINE_BPS / RACK_SIZE / (1 + ACK_SHARE)
PACE_EPS_S = 1e-7          # timing slack so float rounding can't stall the pacer

# Delay-feedback rate control (CHANGED_ROUTER_STAMPING_DESIGN.md). Queueing
# delay q = one-way delay - smallest one-way delay seen recently.
# Feedback is one RTT (~100 ms) old but queues hold only ~5 ms, so any
# overshoot above capacity overflows them before we hear about it. Hence:
# cut hard when q is over target, climb back slowly, and never go above the
# topology fair share.
CC_BETA = 0.8              # cut by BETA * (q - target) / q ...
CC_MAX_CUT = 0.5           # ... but to no less than half, at most once per RTT
CC_AI_BPS = 0.1e6          # additive increase per RTT per flow, below the share
CC_RELAX = 1.1             # carried-over rate creeps back up by this ...
CC_RELAX_AFTER = 3         # ... once this many rounds in a row were clean
MIN_RATE_BPS = 0.5e6
BASE_WINDOW_S = 10.0       # base one-way delay = min over roughly this window
# React to the *standing* queueing delay: the minimum q over the last
# STANDING_SAMPLES ACKs (from the last RTT). Path jitter delays packets
# randomly, so one of them usually gets through fast and the minimum stays
# low; a real queue delays every packet, so the minimum rises with it (the
# same filter as Copa). Counted, not timed: a round's ACKs arrive within
# ~30 ms, and a time window would keep the pre-queue samples all round.
STANDING_SAMPLES = 6
# Delivery rate under congestion: bytes acked / time, counting only ACKs for
# packets that waited in a queue (q > target). While the queue is backed up
# it releases this flow's packets at exactly its share of the bottleneck, the
# quantity HPCC reads from txBytes. ACKs from before the queue built (which
# come back at our own send rate) and from the loss-recovery tail are left out.
# Measured over contiguous runs of queued ACKs (a gap ends a run); the run
# with the most bytes wins.
RUN_MIN_ACKS = 4
RUN_GAP_PKTS = 4           # a gap longer than this many packets at our rate ends a run

_U32 = 1 << 32
_U24_MAX = (1 << 24) - 1


def now_us(t: float) -> int:
    return int(t * 1_000_000) % _U32


def rack_of(name: str) -> int:
    """'sender4' / 'receiver4' -> rack 1."""
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) // RACK_SIZE if digits else -1


def wire_bits(datagram_len: int) -> int:
    return 8 * (datagram_len + WIRE_OVERHEAD)


# ---------------------------------------------------------------------------
# 2. Codec
# ---------------------------------------------------------------------------

# Data packet: flags|#sack (1), seq (4), send_ts (4), then one or more frames.
# Frame: msg_id (4), offset in message (3), len|END (2), then len data bytes.
# A packet can carry pieces of several messages to the same receiver, so
# small messages share one packet's headers and one ACK. seq counts frame
# data bytes only, so ACK/SACK work exactly as with one message per packet.
_DATA = struct.Struct("!BII")
_FRAME = struct.Struct("!I")
# ACK: flags|#sack (1), cum_ack (4), rwnd (2), echo_ts (4), owd (3)
_ACK = struct.Struct("!BIHI")
_SACK = struct.Struct("!II")


def pack_data(seq, ts, frames, probe=False) -> bytes:
    """frames: [(msg_id, offset, data, end), ...]."""
    out = [_DATA.pack((F_PROBE if probe else 0) << 4, seq % _U32, ts)]
    for msg_id, offset, data, end in frames:
        out.append(_FRAME.pack(msg_id))
        out.append(offset.to_bytes(3, "big"))
        out.append((len(data) | (FRAME_END if end else 0)).to_bytes(2, "big"))
        out.append(data)
    return b"".join(out)


def unpack_data(buf: bytes):
    """-> (flags, seq, ts, [(msg_id, offset, data, end), ...])."""
    b0, seq, ts = _DATA.unpack_from(buf)
    frames, i, n = [], DATA_HDR, len(buf)
    while i + FRAME_HDR <= n:
        (msg_id,) = _FRAME.unpack_from(buf, i)
        offset = int.from_bytes(buf[i + 4:i + 7], "big")
        ln = int.from_bytes(buf[i + 7:i + 9], "big")
        end, ln = bool(ln & FRAME_END), ln & ~FRAME_END
        i += FRAME_HDR
        frames.append((msg_id, offset, buf[i:i + ln], end))
        i += ln
    return b0 >> 4, seq, ts, frames


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
    __slots__ = ("seq", "frames", "nbytes", "sent_at", "tx", "pn", "in_retxq")

    def __init__(self, seq, frames):
        self.seq = seq
        self.frames = frames  # [(msg_id, offset, data, end), ...]
        self.nbytes = sum(len(fr[2]) for fr in frames)   # stream bytes covered
        self.sent_at = 0.0
        self.tx = 0
        self.pn = -1          # packet number of the latest transmission
        self.in_retxq = False  # declared lost, waiting in the flow's retx queue

    @property
    def hi(self):
        return self.seq + self.nbytes


class FlowTx:
    def __init__(self, dst: str):
        self.dst = dst
        self.next_seq = 0
        self.queue: list[Seg] = []          # packetized, not yet sent
        self.pending_msgs: list = []        # offered, not yet packetized
        self.unacked: dict[int, Seg] = {}   # seq -> segment
        self.cum = 0
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.latest_rtt = 0.0
        self.min_rtt: float | None = None
        self.reordering_seen = False
        self.spurious = 0                    # retransmissions that weren't needed
        self.backoff = 0
        # Loss detection (QUIC-style). Every transmission, including a
        # retransmission, gets a fresh packet number, so "sent after" is exact.
        self.next_pn = 0
        self.largest_acked_pn = -1
        self.last_send = 0.0
        self.retx = 0
        # Pacing. rate_bps is set at each flush from the topology; next_send is
        # the earliest time this flow may send again at that rate.
        self.cross_rack = False
        self.rate_bps = NIC_BPS
        self.next_send = 0.0
        self.retxq: list[Seg] = []          # lost segments, sent before new data
        # Rate control.
        self.share_bps = NIC_BPS            # this round's topology fair share
        self.carry_bps: float | None = None  # learned cap carried across rounds
        self.cut_this_round = False
        self.retx_at_round_start = 0
        self.clean_rounds = 0
        self.run_first: float | None = None   # current run of queued ACKs
        self.run_last = 0.0
        self.run_bytes = 0
        self.run_acks = 0
        self.best_run_bytes = 0                # longest run so far this round
        self.best_run_rate = 0.0
        self.last_cut = -1e9
        self.min_cur_us: int | None = None  # windowed min of one-way delay:
        self.min_prev_us: int | None = None  # two half-window buckets
        self.min_epoch = 0.0
        self.q_samples: deque[tuple[float, float]] = deque()  # (time, q)
        self.cuts = 0

    def pending(self) -> bool:
        return bool(self.queue or self.retxq)

    def base_owd_s(self) -> float | None:
        vals = [v for v in (self.min_cur_us, self.min_prev_us) if v is not None]
        return min(vals) / 1e6 if vals else None

    def on_delay(self, now: float, owd_us: int, acked_bytes: int):
        """React to the one-way delay the receiver measured for one packet."""
        # Windowed minimum, so the base follows delay_change events.
        if now - self.min_epoch >= BASE_WINDOW_S / 2:
            self.min_prev_us, self.min_cur_us = self.min_cur_us, None
            self.min_epoch = now
        if self.min_cur_us is None or owd_us < self.min_cur_us:
            self.min_cur_us = owd_us
        base = self.base_owd_s()
        rtt = self.srtt or PTO_INIT_S
        # Standing queueing delay: min over a short window, to filter jitter.
        q_raw = max(0.0, owd_us / 1e6 - base)
        if acked_bytes and q_raw > TARGET_DELAY_S:
            gap_limit = max(0.01, RUN_GAP_PKTS * wire_bits(MAX_DATAGRAM) / self.rate_bps)
            if self.run_first is None or now - self.run_last > gap_limit:
                self.run_first, self.run_bytes, self.run_acks = now, 0, 1
            else:
                self.run_bytes += acked_bytes
                self.run_acks += 1
            self.run_last = now
            if (self.run_acks >= RUN_MIN_ACKS and now > self.run_first
                    and self.run_bytes > self.best_run_bytes):
                self.best_run_bytes = self.run_bytes
                self.best_run_rate = 8 * self.run_bytes / (now - self.run_first)
        samples = self.q_samples
        samples.append((now, q_raw))
        horizon = now - rtt
        while samples and (samples[0][0] < horizon or len(samples) > STANDING_SAMPLES):
            samples.popleft()
        if len(samples) < STANDING_SAMPLES:
            return
        q = min(v for _, v in samples)

        if q > TARGET_DELAY_S:
            # Over target: cut, harder the further over, once per RTT.
            if now - self.last_cut >= rtt:
                factor = max(1 - CC_BETA * (q - TARGET_DELAY_S) / q, CC_MAX_CUT)
                self.rate_bps = max(MIN_RATE_BPS, self.rate_bps * factor)
                self.last_cut = now
                self.cut_this_round = True
                self.cuts += 1
        elif acked_bytes and self.pending() and self.rate_bps < self.share_bps:
            # Under target with data to send: climb back by about
            # CC_AI_BPS per RTT, never above the fair share.
            self.rate_bps = min(self.share_bps, self.rate_bps
                                + CC_AI_BPS * 8 * acked_bytes / (self.rate_bps * rtt))

    def start_round(self, share_bps: float):
        """New data arrived: start at the fair share, or below it if the last
        round showed the path can't take it (a slow link stays slow)."""
        # Carry a cut only if the round also lost packets: an overloaded link
        # overflows and drops, while delay noise (jitter) never does. Spurious
        # retransmissions (reordering mistaken for loss) don't count.
        if self.cut_this_round and self.real_losses() > self.retx_at_round_start:
            # The queue was backed up, so ACKs came back at this flow's share
            # of the bottleneck: start the next round there.
            carry = self.rate_bps
            if self.best_run_rate > 0:
                carry = min(carry, self.best_run_rate)
            self.carry_bps = max(MIN_RATE_BPS, carry)
            self.clean_rounds = 0
        elif self.carry_bps is not None:
            # A link that's exactly full looks the same as one with room to
            # spare (no queue either way), so only creep back up after a few
            # clean rounds, or a permanently slow link gets overloaded again.
            self.clean_rounds += 1
            if self.clean_rounds >= CC_RELAX_AFTER:
                self.carry_bps *= CC_RELAX
        if self.carry_bps is not None and self.carry_bps >= share_bps:
            self.carry_bps = None
        self.cut_this_round = False
        self.retx_at_round_start = self.real_losses()
        self.run_first = None
        self.best_run_bytes, self.best_run_rate = 0, 0.0
        self.share_bps = share_bps
        self.rate_bps = min(share_bps, self.carry_bps or share_bps)

    def real_losses(self) -> int:
        return self.retx - self.spurious

    def reo_wnd(self) -> float:
        if not self.reordering_seen or self.min_rtt is None:
            return 0.0
        return min(self.srtt or self.min_rtt, REO_WND_RTT_FRAC * self.min_rtt)

    def loss_delay(self) -> float:
        rtt = self.latest_rtt or self.srtt
        if not rtt:
            return RTO_INIT_S
        return rtt + self.reo_wnd() + LOSS_TIME_MIN_S

    def detect_lost(self, now: float) -> tuple[list[Seg], float | None]:
        """Segments to declare lost now, and when the next one would be."""
        lost, nxt = [], None
        if self.largest_acked_pn < 0:
            return lost, nxt
        delay = self.loss_delay()
        for s in self.unacked.values():
            if s.pn >= self.largest_acked_pn:
                continue
            if (PKT_THRESH and self.largest_acked_pn - s.pn >= PKT_THRESH) \
                    or now - s.sent_at >= delay - 1e-6:
                lost.append(s)
            else:
                t = s.sent_at + delay
                nxt = t if nxt is None else min(nxt, t)
        return lost, nxt

    def pto(self) -> float:
        if self.srtt is None:
            base = PTO_INIT_S
        else:
            base = self.srtt + max(4 * self.rttvar, PTO_GRANULARITY_S) + TARGET_DELAY_S
        cap = PTO_MAX_RTTS * (self.srtt or PTO_INIT_S)
        return min(base * (2 ** self.backoff), max(base, cap))

    def packetize(self):
        """Pack every pending message into full datagrams, in order. A large
        message spans several packets; small ones share packets (a message
        may also be split across the boundary of two packets)."""
        frames, room = [], MAX_DATAGRAM - DATA_HDR

        def close():
            nonlocal frames, room
            if frames:
                seg = Seg(self.next_seq, frames)
                self.next_seq += seg.nbytes
                self.queue.append(seg)
            frames, room = [], MAX_DATAGRAM - DATA_HDR

        for msg in self.pending_msgs:
            data, n, off = msg.payload, msg.size, 0
            while True:
                if room - FRAME_HDR < min(MIN_FRAME_DATA, n - off) or room <= FRAME_HDR:
                    close()
                take = min(n - off, room - FRAME_HDR)
                end = off + take >= n
                frames.append((msg.msg_id, off, data[off:off + take], end))
                room -= FRAME_HDR + take
                off += take
                if end:
                    break
        close()
        self.pending_msgs = []

    def on_rtt(self, rtt: float):
        self.latest_rtt = rtt
        if self.min_rtt is None or rtt < self.min_rtt:
            self.min_rtt = rtt
        if self.srtt is None:
            self.srtt, self.rttvar = rtt, rtt / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt)
            self.srtt = 0.875 * self.srtt + 0.125 * rtt

    def on_ack(self, cum: int, sacks, now: float = 0.0) -> int:
        """Drop acknowledged segments (cumulative or SACKed; the sender keeps
        its own scoreboard, so a range reported once stays acked). Returns
        bytes newly acknowledged. Also notices reordering, which widens the
        loss-detection window (see REO_WND_RTT_FRAC)."""
        if cum > self.cum:
            self.cum = cum
        acked = 0
        prev_largest = self.largest_acked_pn
        for seq in list(self.unacked):
            s = self.unacked[seq]
            if s.hi <= self.cum or any(l <= s.seq and s.hi <= r for l, r in sacks):
                acked += s.nbytes
                if s.tx == 1 and s.pn < prev_largest:
                    # Sent earlier, never resent, yet acked after a later one.
                    self.reordering_seen = True
                elif (s.tx > 1 and self.min_rtt is not None
                        and now - s.sent_at < SPURIOUS_RTT_FRAC * self.min_rtt):
                    # Too fast to be the retransmission: the original arrived
                    # late, so the retransmission was spurious.
                    self.reordering_seen = True
                    self.spurious += 1
                if s.pn > self.largest_acked_pn:
                    self.largest_acked_pn = s.pn
                del self.unacked[seq]
        return acked


# ---------------------------------------------------------------------------
# 5. Sender
# ---------------------------------------------------------------------------

class MySender(Sender):
    def on_start(self):
        self.flows: dict[str, FlowTx] = {}
        self.addr_to_name = {_norm(a): n for n, a in self.peers.items()}
        self.my_rack = rack_of(self.name)
        # When our NIC finishes the last datagram we handed it. The pacer never
        # gets ahead of this, so the NIC queue stays near empty and a
        # retransmission can't end up stuck behind a backlog of new data there.
        self.nic_free_at = 0.0

    def _flow(self, dst: str) -> FlowTx:
        f = self.flows.get(dst)
        if f is None:
            f = self.flows[dst] = FlowTx(dst)
            f.cross_rack = rack_of(dst) != self.my_rack
        return f

    def on_app_data(self, msg):
        f = self._flow(msg.dst)
        f.pending_msgs.append(msg)
        # Gather the whole round before sending (every message of a round is
        # offered at the same instant).
        if not self.timer_pending("flush"):
            self.set_timer("flush", 0.0)

    # ---- starting rates (CHANGED_SLOW_START_DESIGN.md) ----------------------

    def _set_rates(self):
        """Fair share per flow, assuming every server is doing the same
        all-to-all round. Flows to other racks split this sender's slice of
        the rack->spine link; flows within the rack split the rest of the NIC.
        Delay feedback (M4) will correct these when the assumption is wrong."""
        active = [f for f in self.flows.values() if f.queue]
        cross = [f for f in active if f.cross_rack]
        local = [f for f in active if not f.cross_rack]
        cross_total = min(CROSS_RACK_BPS, NIC_BPS) if cross else 0.0
        local_total = NIC_BPS - cross_total
        now = self.now()
        for f in cross:
            f.start_round(cross_total / len(cross))
        for f in local:
            f.start_round(local_total / len(local))
        for f in cross:
            self.log(f"round {f.dst}: start {f.rate_bps / 1e6:.2f} carry "
                     f"{(f.carry_bps or 0) / 1e6:.2f} cuts {f.cuts} retx {f.retx}")
        for f in active:
            f.next_send = max(f.next_send, now)

    # ---- pacer ---------------------------------------------------------------

    def _send_seg(self, f: FlowTx, s: Seg, probe=False):
        t = self.now()
        s.sent_at = t
        s.tx += 1
        s.pn = f.next_pn
        f.next_pn += 1
        f.last_send = t
        if s.tx > 1:
            f.retx += 1
        pkt = pack_data(s.seq, now_us(t), s.frames, probe)
        self.nic_free_at = max(self.nic_free_at, t) + wire_bits(len(pkt)) / NIC_BPS
        self.send_datagram(pkt, to=f.dst)
        return len(pkt)

    def _pick(self, now: float):
        """Next (flow, is_retx) to send, or (None, False). Retransmissions go
        first: a round ends when its last message completes, and the message
        with the hole can't complete until its retransmission arrives."""
        for f in self.flows.values():
            if f.retxq:
                return f, True
        best = None
        for f in self.flows.values():
            if f.queue and f.next_send <= now + PACE_EPS_S:
                if best is None or f.next_send < best.next_send:
                    best = f
        return best, False

    def _pump(self):
        """Send whatever the pacer allows now, then sleep until the next send."""
        now = self.now()
        while self.nic_free_at <= now + PACE_EPS_S:
            f, retx = self._pick(now)
            if f is None:
                break
            if retx:
                s = f.retxq.pop(0)
                s.in_retxq = False
                if s.seq not in f.unacked:   # acked while it waited
                    continue
            else:
                s = f.queue.pop(0)
                f.unacked[s.seq] = s
            nbytes = self._send_seg(f, s)
            f.next_send = max(f.next_send, now) + wire_bits(nbytes) / f.rate_bps
            if not self.timer_pending("pto:" + f.dst):
                self._arm(f)

        # Next wakeup: when the NIC is free, and (for new data) when some
        # flow's rate allows its next packet.
        if any(f.retxq for f in self.flows.values()):
            at = self.nic_free_at
        else:
            waits = [f.next_send for f in self.flows.values() if f.queue]
            if not waits:
                self.cancel_timer("pace")
                return
            at = max(self.nic_free_at, min(waits))
        self.set_timer("pace", max(0.0, at - now))

    def _flush(self):
        for f in self.flows.values():
            if f.pending_msgs:
                f.packetize()
        self._set_rates()
        self._pump()

    # ---- loss recovery -------------------------------------------------------

    def _arm(self, f: FlowTx):
        """One timer per flow: the earlier of the next time-threshold loss
        and the probe timeout (measured from the last send)."""
        name = "pto:" + f.dst
        if not f.unacked:
            self.cancel_timer(name)
            return
        now = self.now()
        _, loss_at = f.detect_lost(now)
        at = f.last_send + f.pto()
        if loss_at is not None:
            at = min(at, loss_at)
        # Floor keeps float rounding from spinning a zero-delay timer.
        self.set_timer(name, max(TIMER_FLOOR_S, at - now))

    def _recover(self, f: FlowTx) -> bool:
        """Queue everything detection calls lost for retransmission, ahead of
        new data. True if anything is lost."""
        lost, _ = f.detect_lost(self.now())
        added = False
        for s in sorted(lost, key=lambda s: s.seq):
            if not s.in_retxq:
                s.in_retxq = True
                f.retxq.append(s)
                added = True
        if added:
            self._pump()
        return bool(lost)

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
        acked = f.on_ack(cum, sacks, self.now())
        f.on_delay(self.now(), owd, acked)
        if acked:
            f.backoff = 0
            self._recover(f)
        self._arm(f)

    def on_timer(self, name):
        if name == "flush":
            self._flush()
        elif name == "pace":
            self._pump()
        elif name.startswith("pto:"):
            f = self.flows.get(name[4:])
            if f is None or not f.unacked:
                return
            if not self._recover(f):
                now = self.now()
                if f.pending() or now < f.last_send + f.pto() - PACE_EPS_S:
                    # Still sending (or sent recently): there's no tail yet
                    # to probe, so just look again later.
                    self._arm(f)
                    return
                # Tail-loss probe: nothing is provably lost, so resend the
                # newest unacked segment. Its ACK's cum/SACK shows exactly
                # what else is missing, and detection takes it from there.
                newest = max(f.unacked.values(), key=lambda s: s.pn)
                self._send_seg(f, newest, probe=True)
                f.backoff = min(f.backoff + 1, 6)
            self._arm(f)

    def on_stop(self):
        for f in self.flows.values():
            self.log(f"flow {f.dst}: rate {f.rate_bps / 1e6:.2f} Mbps "
                     f"share {f.share_bps / 1e6:.2f} cuts {f.cuts} retx {f.retx}")


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
        _flags, seq, ts, frames = unpack_data(data)
        key = _norm(addr)
        f = self.flows.get(key)
        if f is None:
            f = self.flows[key] = FlowRx()
        f.stream.add(seq, seq + sum(len(fr[2]) for fr in frames))

        for msg_id, offset, payload, end in frames:
            if msg_id in f.done or self.delivered(msg_id):
                continue
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
