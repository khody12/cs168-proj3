# CS168 Project 3: A Transport Protocol for the Data Center Backbone

Ellis O'Dowd, Eric Khodorenko

This README describes our protocol as built, in implementation detail. It
starts from our design document (an HPCC-style protocol with SACK and
immediate acks) and changes it where the simulator, the topology, or the
grading math call for it. Each change is listed in
[section 3](#3-what-changes-from-the-design-doc-and-why); current results are
in [section 8](#8-test-plan-and-current-results).

Short, high-level explanations of the biggest changes:

- [CHANGED_ROUTER_STAMPING_DESIGN.md](CHANGED_ROUTER_STAMPING_DESIGN.md): why
  switches can't stamp hop records, and how endpoints measure congestion instead
- [CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md): why our header
  sits inside UDP, which fields we dropped and added
- [CHANGED_HANDSHAKE_DESIGN.md](CHANGED_HANDSHAKE_DESIGN.md): why there's no
  SYN/FIN handshake, and what replaces it
- [CHANGED_SLOW_START_DESIGN.md](CHANGED_SLOW_START_DESIGN.md): why slow start
  doesn't work here, and how we pick and pace the starting rate from the
  topology
- [CHANGED_LOSS_RECOVERY_DESIGN.md](CHANGED_LOSS_RECOVERY_DESIGN.md): why one
  lost packet nearly costs a round, and how we detect, probe for and resend
  losses (including two copies when the loss looks random)

The course's setup and run instructions are at the [bottom](#course-setup-and-running).

---

## 1. Goals

The datacenter scenario is graded on three requirements. Each one is measured
across seeds, and only the worst seed counts.

| Metric | Requirement | Points | What it means for us |
|---|---|---|---|
| `reliability.min_ratio` | 1.0 | 40% | Every message must arrive with the exact bytes. One lost message fails it. |
| `step_slowdown.mean_max` | ≤ 2.5 | 45% | A round's time to its *last* delivery, divided by the ideal, averaged over rounds. The tail is all that counts. |
| `overhead.max_ratio` | ≤ 1.30 | 15% | All wire bytes from both ends (IP/UDP 28 B, our headers, acks, retransmits) divided by delivered app bytes. |

Reference point: the stop-and-wait baseline on `basic_workload` scores
**reliability 1.0, slowdown 54.7, overhead 1.05**.

## 2. The network, in numbers

```
 sender0-2 ──100M/20ms──▶ rack0 ──150M/5ms──▶ ┌───────┐ ──150M/5ms──▶ rack1 ──100M/20ms──▶ receiver3-5
 (queue 1000 pkts)       (queue 64)           │ spine │  (queue 64)         (queue 64)
                                              └───────┘
 9 servers, 3 per rack. Server N runs both senderN and receiverN (separate endpoints).
 One-way delay: 40 ms within a rack, 50 ms across racks. RTT 80 / 100 ms.
 (The simulation is 100x slower than the real network: 100 Mbps / 100 ms stands in for 10 Gbps / 1 ms.)
```

Workload: each round, every server sends `burst_bytes` to each of the 8 others,
all at the same instant. A round ends when its last message arrives.

### How the ideal is computed (`transportlab/metrics.py::_steps`)

```
ideal = longest one-way path delay  +  max over links ( bytes crossing that link / link bandwidth )
```

For all-to-all traffic with B bytes per pair, the busiest links are the
**rack↔spine links**, not the servers' own links:

| Link | Bytes per round | Bandwidth | Bytes ÷ bandwidth |
|---|---|---|---|
| server → rack (uplink) | 8B | 100 Mbps | 0.080·B |
| rack → server (downlink) | 8B | 100 Mbps | 0.080·B |
| **rack → spine / spine → rack** | **18B** (3 senders × 6 remote dsts) | 150 Mbps | **0.120·B** ← bottleneck |

`basic_workload` (B = 5600, four 1400-byte messages per pair):
`ideal = 50 ms + 18·4·1428·8 / 150 Mbps = 50 + 5.5 = 55.5 ms`. This matches what
the harness reports. To score 2.5 we need a mean step of **139 ms or less**,
which is less than 1.5 RTTs.

### Three facts that drive the design

1. **The ideal is about half an RTT.** In the ideal, the data arrives after a
   one-way delay. Anything that costs an extra RTT (a handshake, or one
   retransmission of a tail packet) adds 100 ms to a 55 ms ideal, so that round
   scores about 2.8 or worse.
2. **BDP ≫ buffers.** One sender's bandwidth-delay product is
   100 Mbps × 100 ms ≈ 1.25 MB ≈ 860 packets. The switch queues hold 64
   packets (5.1 ms at 150 Mbps). The buffers can't absorb a window's worth of
   burst.
3. **Most rounds finish within one RTT.** A sender's 8·B bytes take
   8·B·8/100 Mbps to serialize. That is under one RTT for any B below
   ~150 KB per pair. Feedback from the network arrives *after* most of the round
   has been sent. That makes the sending rate of the **first** RTT the most
   important decision. In a classic slow start or an HPCC-style reaction, the
   first RTT is guesswork.

Worked example of fact 3: B = 28 KB (20 pkts/pair). Without pacing, each sender
pushes 160 packets out at 100 Mbps, and 75% of them cross the spine. The three
senders in a rack offer about 225 Mbps to a 150 Mbps uplink for about 18 ms.
That overflows the 64-packet queue by roughly 50 packets. Every one of those
drops is a retransmission (overhead) and probably an extra RTT on the tail
(slowdown).

## 3. What changes from the design doc, and why

| Design doc | What we built | Why |
|---|---|---|
| Switches stamp hop records (switch ID, qlen, txBytes, BW) into packets, HPCC-style | **Endpoints measure one-way queueing delay** with the shared clock; the receiver echoes it in every ACK, and the sender's rate controller acts on it (§5.3) | ns-3 switches here are plain drop-tail routers (`sim/tlsim.cc`). Only `protocol.py` runs, and only on hosts, so nothing can write hop records en route. Every host shares one clock, so `receiver_now − send_ts` is the exact forward one-way delay; subtracting the base delay gives the queueing on the path. See [CHANGED_ROUTER_STAMPING_DESIGN.md](CHANGED_ROUTER_STAMPING_DESIGN.md). |
| SYN / SYN-ACK / FIN handshake (timing diagram) | **0-RTT, connectionless.** The first packet of a flow carries data. No FIN. | A handshake adds 100 ms to a 55 ms ideal, which alone is a slowdown of about 2.8 (fact 1). See [CHANGED_HANDSHAKE_DESIGN.md](CHANGED_HANDSHAKE_DESIGN.md). |
| Slow start, then ramp quickly | **Start at a topology fair-share rate (minus room for ACKs), paced from the first packet.** Delay feedback can only lower it. | Fact 3: most rounds are over before slow start would get going. See [CHANGED_SLOW_START_DESIGN.md](CHANGED_SLOW_START_DESIGN.md) and §5.3. |
| Byte-offset sequence numbers + cumulative ACK + SACK | **Kept** on the wire. The sender also numbers each *transmission* internally (never sent) to know which was sent later. | Works as designed. Retransmissions reuse their byte range and carry a fresh send timestamp, so delay samples stay unambiguous. |
| Source/dest ports, checksum | **Dropped** | The harness only sends UDP, and UDP's header already carries ports and a checksum covering our whole header and payload. See [CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md). |
| Receive window | **Kept, in ACKs only** | Never binding in the simulator, but a real receiver needs flow control. |
| Up to 15 SACK blocks × 8 B | **Same format; the receiver includes at most `MAX_SACK` = 4** | We ACK every packet, so older holes were already reported in earlier ACKs. Fewer blocks keep ACKs small during loss. |
| One header for data and ACKs | **Separate data and ACK headers** sharing the flags byte | Senders and receivers are separate endpoints, so data packets never carry ACK info and ACKs never carry data. |
| SACK + short retransmit timer (tradeoffs #2, #5) | **Kept, plus:** RACK loss detection, a tail-loss probe timed from each flow's last segment, and two copies of a retransmission when the loss looks random | One lost packet already makes a small round ~2.3× its ideal, and a second loss ~5×. See [CHANGED_LOSS_RECOVERY_DESIGN.md](CHANGED_LOSS_RECOVERY_DESIGN.md). |
| Immediate ack for every packet | **Kept** | Costs about 3% overhead (§6). We have the budget, and every ACK is a fresh delay sample for the rate controller. Ack-every-2 hasn't been needed. |
| One message per packet (implied) | **Packets carry "frames"**: one or more `(msg_id, offset, length, END)` pieces | Small messages share a packet's headers and its ACK (100-byte messages: overhead 1.86 → 1.16). Large messages are split across packets. Each message is delivered as soon as its own pieces arrive. |

What we keep from the doc: the **core idea**. Senders learn queue state from
the path, receivers echo it in every ACK, and senders set their rate from it
to avoid drops before they happen. We also keep **byte sequence numbers,
cumulative ACK + SACK**, the **receive window**, **immediate acks**, and an
**RTO near the real RTT**.

## 4. Wire format

Everything below sits inside UDP. The harness only sends UDP datagrams, and
UDP's header already supplies the ports and checksum (see
[CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md)). All integers
are big-endian (`struct` with `!`). Timestamps are microseconds of
`self.now()`, stored mod 2³². That wraps after 71 minutes, and a trial lasts
seconds.

The first byte is the same in both packet types: 4 flag bits, then a 4-bit
SACK count, the same layout as the design doc's flags/#SACK fields.

| Flag | Meaning |
|---|---|
| `ACK` | this is an ACK packet (otherwise it's data) |
| `PROBE` | data: sent by the tail-loss probe timer |
| (2 spare) | reserved, 0 |

A message's END marker is not a packet flag; it's the top bit of each frame's
length (§4.1), because one packet can hold the ends of several messages.

### 4.1 Data packet: 9-byte header + frames

| Field | Bytes | Description |
|---|---|---|
| flags \| #SACK | 1 | `ACK` = 0; #SACK = 0 |
| sequence number | 4 | byte offset of this packet's first data byte in the sender→receiver stream (as in the design doc). The stream counts frame *data* bytes only |
| send timestamp | 4 | µs, when the sender called `send_datagram`. A retransmission gets a fresh timestamp |
| **frame** × n | 9 + data | one piece of one message, repeated until the packet is full |

Frame:

| Field | Bytes | Description |
|---|---|---|
| msg_id | 4 | the harness's message ID (globally unique) |
| offset in message | 3 | where this piece goes in its message (messages ≤ 16 MB) |
| length \| END | 2 | bits 0–14: data length; bit 15 (`END`): this piece holds the message's last byte, so `offset + length` is its total size |
| data | length | |

Packing (sender, at flush): each flow's pending messages are packed in offer
order into datagrams of up to 1472 B. A large message is split across
packets, and a small one may straddle the boundary between two packets. A new
frame isn't started in less than 64 B of room (`MIN_FRAME_DATA`).

Sizes: a lone 1400-byte message costs an 18-byte header (9 + 9); one frame can
carry up to 1454 B. 100-byte messages fit 13 to a packet (109 B each).

### 4.2 ACK packet: 14 bytes + 8 per SACK block

| Field | Bytes | Description |
|---|---|---|
| flags \| #SACK | 1 | `ACK` = 1; #SACK = number of blocks that follow (0 to `MAX_SACK`) |
| cumulative ACK | 4 | next byte expected; every byte before it has arrived |
| receive window | 2 | free buffer space in 256-byte units (as in the design doc) |
| echo timestamp | 4 | send timestamp of the data packet that triggered this ACK, for RTT |
| forward OWD | 3 | µs, `receiver.now() − send_ts` for that packet. **Replaces the echoed hop records** |
| SACK block × n | 8 | `(left, right)` byte offsets of a received range above the cumulative ACK; the block holding the triggering packet first, then highest first |

The usual ACK is 14 B, or 42 B on the wire. The doc's version was
18 + 8k + 33 bytes of echoed hop records.

## 5. Endpoint behavior

### 5.1 Flows and state

- A **flow** is one (sender endpoint → receiver) pair. Each sender has at most
  8 flows.
- Sender state per flow:
  - **data:** messages offered but not yet packed; packed segments not yet
    sent; sent-and-unacked segments `seq → (frames, send time, packet
    number)`; a retransmission queue.
  - **rate:** this round's fair share, current rate, a rate carried over from
    a congested round, the next time the pacer may send for this flow, recent
    queueing-delay samples, the windowed minimum one-way delay.
  - **timing:** srtt / rttvar / latest / min RTT, whether reordering has been
    seen, spurious-retransmission count, probe backoff.
- Receiver state per source address: received stream bytes as a range set
  (gives the cumulative ACK and the SACK blocks); per message, its pieces plus
  received byte intervals; the set of completed `msg_id`s (also checked with
  `self.delivered(msg_id)`).

### 5.2 Sending: gather, pack, pace

1. `on_app_data` adds the message to its flow's pending list and sets a
   **0-delay "flush" timer**. All messages of a round are offered at the same
   simulated instant, so this lets us see the whole round before choosing a
   send order.
2. **Flush** packs each flow's pending messages into segments (§4.1), sets
   each active flow's rate for the round (§5.3), and starts the pacer.
3. **The pacer** is one `"pace"` timer per sender. Each flow has a `next_send`
   time; after sending a datagram it moves forward by
   `wire_bits / flow_rate`. On each tick the pacer sends:
   - first, any flow's queued **retransmission**: a round ends when its last
     message completes, and the message with the hole can't complete until
     its retransmission arrives;
   - otherwise, new data from the flow whose `next_send` is earliest and due.
4. **The NIC never gets a backlog.** The sender tracks when its NIC will finish
   the last datagram it was handed (`nic_free_at`, at 100 Mbps) and doesn't
   hand it another before then. The NIC's own 1000-packet queue stays empty,
   so a retransmission can't get stuck behind new data there.

### 5.3 Rate control (our endpoint-only HPCC)

**Starting rate (the prior).** At each flush, each active flow gets the share
it would get if every server were doing all-to-all:

- **cross-rack:** this sender's slice of its rack's 150 Mbps link to the
  spine, shared by 3 senders, **minus room for ACKs**: that link also carries
  the ACKs for all data coming into the rack, about 3% extra
  (`ACK_SHARE` = 42 B per 1500 B). So `150 / 3 / 1.029 ≈ 48.6 Mbps`, split
  over the active cross-rack flows (≈ 8.1 Mbps each for 6).
- **intra-rack:** the rest of the NIC (≈ 51.4 Mbps), split over the active
  same-rack flows (≈ 25.7 Mbps each for 2).

The rack of a host is `index // 3`, read from its name. Without the ACK room,
long rounds overloaded the spine links by ~3% and slowly overflowed them
(large_incast: 62 drops, slowdown 1.73; with it: 0 drops, 1.05).

**Feedback, on every ACK** (`FlowTx.on_delay`):

```
base   = min one-way delay over the last ~10 s (two 5-s buckets, follows delay_change)
q_raw  = this packet's one-way delay − base
q      = min(q_raw over the last 6 ACKs, within one RTT)     # "standing" queue delay

if q > T (2.5 ms, about half a spine queue):
    at most once per RTT:  rate *= max(0.5, 1 − 0.8·(q − T)/q)
elif data is waiting and rate < share:
    rate += 0.1 Mbps per RTT (spread over the ACKs)          # never above share
rate stays within [0.5 Mbps, share]
```

- **Why a standing minimum, not each sample:** path jitter delays individual
  packets at random, so one of 6 usually gets through quickly and the minimum
  stays near zero. A real queue delays every packet, so the minimum rises with
  it. This is Copa's filter. Counted rather than timed, because a round's ACKs
  arrive within ~30 ms: a time window kept the early, pre-queue samples all
  round and never saw the queue.
- **Why never above the share, and why climbing back is slow:** feedback is
  one RTT (~100 ms) old, and queues hold ~5 ms. Any overshoot above capacity
  overflows a queue before the sender can hear about it.

**Across rounds: remembering a slow link** (`FlowTx.start_round`). Rounds are
seconds apart and usually shorter than an RTT, so a round can't fix its own
rate in time. If the previous round both **cut the rate** and had **real
losses** (retransmissions, not counting spurious ones), the next round starts
at the **measured delivery rate**:

- bytes acked ÷ time over the longest contiguous run of ACKs whose packets saw
  `q_raw > T` (≥ 4 ACKs; a gap longer than 4 packets' time ends a run);
- while a queue is backed up, it releases this flow's packets at exactly its
  share of the bottleneck. This is the quantity HPCC reads from `txBytes`.

The carried rate creeps back up (×1.1 per round) only after 3 clean rounds in
a row, because a link that's exactly full looks the same as one with room to
spare.

Alternatives we tried and dropped (each measured on the sweep):

| Tried | Problem |
|---|---|
| React to each packet's delay | jitter looked like queueing: rates collapsed to the floor and the cuts carried forward (jitter: 1.07 → 4.40) |
| Standing minimum over a time window | missed queues that build within a short round (slow_spine: 2.98) |
| Carry the mildly cut rate | took ~4 rounds to settle on a slow link, losing packets each round |
| Delivery rate = a round's bytes ÷ its whole ACK span | the loss-recovery tail stretched the span: ~10× too low |
| Delivery rate = max over groups of 8 ACKs | captured the pre-queue ACKs at our own send rate: no reduction at all |
| Relax the carried rate after every clean round | re-overloaded a permanently slow link every other round |
| Carry on any retransmission | spurious retransmissions under jitter counted as loss (jitter: 1.08 → 1.61) |

### 5.4 Loss recovery

- **RACK-style detection (RFC 8985).** Every transmission gets an internal
  packet number. A segment is lost once a segment *sent after it* has been
  acked and it's older than `latest_rtt + reo_wnd + 1 ms`.
  - `reo_wnd` is **0 until reordering is seen**. A flow's packets share one
    FIFO path, so without reordering, a later packet's ACK proves an earlier
    one is lost, and the retransmission goes out as soon as that ACK arrives.
  - **Reordering is seen** when a never-retransmitted segment is acked after a
    later one, or a retransmission is "acked" faster than `0.8 · min_rtt`
    (so the original arrived late, and the retransmission was spurious).
    Then `reo_wnd = min(srtt, min_rtt / 4)`, as in RACK.
  - Packet-count thresholds are off: jitter reordering made them fire falsely.
- **Retransmissions** wait in the flow's retransmission queue and the pacer
  sends them before new data, with the **same sequence number and a fresh send
  timestamp**.
- **Tail loss probe, timed from the tail.** The flow's highest unacked
  segment (its *tail*) has nothing after it to reveal a loss, and the tail is
  exactly what step time measures. The probe timer fires at
  `tail.sent_at + srtt + max(4·rttvar, 2 ms) + 2.5 ms`, about 1.1 × RTT
  (≈ 110 ms in sim, ≈ 1.1 ms real), but not while the flow still has data
  waiting to be paced out. It counts from the tail's own send time, not the
  flow's last send of anything: otherwise retransmitting an earlier hole
  pushed the tail's deadline back a whole RTT.
  - A probe resends, immediately and with the `PROBE` flag: **all**
    unacked segments if there are fewer than `DUP_MAX_BURST` (3), since they
    were all sent before the tail and are all overdue (this catches a flow
    losing its last two packets); otherwise just the tail. Its ACK's
    cumulative ack and SACK blocks show exactly what else is missing.
- **Backoff during outages.** Consecutive probes double the timer, capped at
  4 × srtt; any new ACK resets it. During a blackout each flow sends one probe
  per interval, not a window of retransmits.
- **Two copies when the loss looks random** (`DUP_RETX`). Recovery has a
  floor: any loss costs about one RTT, so a small round that loses a packet
  lands near 2.3× the ideal, and one that loses the retransmission too lands
  near 5×. So a retransmission (or a first probe) goes out as **two copies**,
  both lost only with probability loss², unless:
  - the flow had a **standing queue** over target when the loss was detected:
    it was a congestion drop, and an extra copy adds load to the overflowing
    link;
  - **`DUP_MAX_BURST` (3) or more** of the flow's segments are lost at once:
    random loss is scattered, so a burst means an outage (a dead link has no
    queue either, so the first check can't catch it), and doubling a whole
    burst floods the path as it comes back;
  - it's a **backed-off probe** into a path that has gone silent.

  Only retransmissions are doubled, so a clean network pays nothing.
  Doubling everything helped loss but made slow_spine (2.01 → 2.15) and
  server_failure (1.13 → 1.29) worse; the three exceptions removed exactly
  that damage. See
  [CHANGED_LOSS_RECOVERY_DESIGN.md](CHANGED_LOSS_RECOVERY_DESIGN.md).
- **Not built: parity packets (FEC).** They'd rebuild a lost packet with no
  round trip and help at 5–10% loss, but datacenter transports generally don't
  use them: real datacenter loss is mostly congestion (which pacing and delay
  feedback prevent), links already correct bit errors with link-layer FEC, and
  a lossy link is routed around.

### 5.5 Receiver

On every data packet:

1. Add `[seq, seq + data bytes)` to the flow's received range set.
2. For each frame whose message isn't already complete, store the piece at
   `offset`. When a message's received intervals cover `[0, total)` (total
   known once its `END` piece arrives), call `self.deliver(msg_id, bytes)`,
   mark it done, and free its buffer. This doesn't wait for other messages.
3. **Always ACK**, including duplicates, or a lost ACK could wedge the sender.
   The ACK carries the cumulative ack (end of the first contiguous range), the
   receive window, up to `MAX_SACK` blocks above it, the echo timestamp, and
   the forward OWD.

## 6. Overhead budget

1400-byte messages (the basic workload), 1 message per datagram, 1 ACK per
datagram:

| Item | Wire bytes per message | Ratio contribution |
|---|---|---|
| payload | 1400 | 1.000 |
| IP/UDP | 28 | 0.020 |
| our data header (9 + one 9-byte frame) | 18 | 0.013 |
| ACK (28 + 14, no SACK blocks) | 42 | 0.030 |
| **Total, no loss** | **1488** | **≈ 1.063** (measured: 1.063) |
| Budget | 1820 | 1.30 |

That leaves about **330 B per message, or ~22% of packets, for
retransmissions and probes**.

**Small messages:** sent alone, a 100-byte message would cost
100 + 18 + 28 + 42 = 188 B (1.88). Packed 13 to a packet, with one ACK per
packet, the measured ratio is **1.16**.

## 7. Implementation

The course only accepts `protocol.py`, so all of the code lives in that one
file, organized into sections:

1. **Constants / topology**: link rates, rack size, `rack_of(name)`, ACK room,
   rate-control and loss-detection parameters (each commented with why).
2. **Codec**: `pack_data` / `unpack_data` (header + frames), `pack_ack` /
   `unpack_ack`. Unit tests in `tests/test_protocol.py` round-trip them and
   check packing.
3. **`RangeSet`**: sorted disjoint intervals. Used for received stream bytes,
   SACK, and message byte coverage.
4. **`FlowTx`**: per-flow sender state, packing, RACK loss detection, the
   probe timeout, and the rate controller (`on_delay`, `start_round`).
5. **`MySender`**: flush, starting rates, the pacer, probe timers (one per
   flow), ACK handling.
6. **`MyReceiver`**: frame parsing, reassembly, delivery, ACK generation.

Status:

| Piece | Status |
|---|---|
| Codec, framing, reassembly, per-packet ACK, RTO | done (Ellis) |
| SACK + RACK loss detection, tail-loss probe, backoff | done (Ellis); adaptive reordering window added later |
| Pacer with topology starting rates, retransmissions first | done |
| Delay-feedback rate control, carried across rounds | done |
| Packing several messages per packet | done |
| Room for ACKs on the spine links | done |
| Ack-every-2 | not needed so far |
| Tail probe timed from the tail; probe all of a few outstanding segments | done |
| Two copies of retransmissions for random-looking losses | done |
| Parity packets (FEC) | not built; see §5.4 |

## 8. Test plan and current results

Custom workloads live in `workloads/` and run with
`make run SCENARIO=datacenter WORKLOAD=workloads/<name>.yaml SEED=n`.
`tools/sweep.py` runs every workload with seeds 1–5 and prints the **worst**
value of each metric, which is how grading works. Only `basic` is an official
workload; the rest are our guesses at what "harder" graded workloads contain,
built from every event type the harness supports.

| Workload | Setup | What it checks | Slowdown | Overhead | Pass |
|---|---|---|---|---|---|
| basic | given: 2 rounds, 5.6 KB/pair | correctness, 0-RTT | 1.03 | 1.06 | ✅ |
| medium_incast | 28 KB/pair, 5 rounds | pacing in the first RTT | 1.03 | 1.06 | ✅ |
| large_incast | 140 KB/pair, 5 rounds | multi-RTT rounds; ACK room | 1.05 | 1.06 | ✅ |
| small_msgs | 100-byte messages | packing; overhead | 1.01 | 1.16 | ✅ |
| large_msgs | 64 KB messages | splitting / reassembly | 1.05 | 1.06 | ✅ |
| staggered | incast with `jitter_s: 0.02` | starts that aren't simultaneous | 1.21 | 1.06 | ✅ |
| jitter | `delay_change` ±5 ms on a spine link | reordering; jitter filter | 1.08 | 1.07 | ✅ |
| server_failure | `blackout` of server 4's links for 1 s | backoff and recovery | 1.13 | 1.12 | ✅ |
| slow_spine | `bw_change` rack1–spine → 75 Mbps | delay feedback; carried rate | 2.01 | 1.10 | ✅ |
| loss_1 | 1% `loss_burst` on rack0–spine | loss recovery | 2.35 | 1.07 | ✅ |
| loss_server_link | 5% loss on one server's downlink | loss recovery | 2.52 | 1.08 | ❌ (just over) |
| loss_5 / loss_10 | 5% / 10% loss on rack0–spine | loss recovery | 2.92 / 3.86 | 1.12 / 1.18 | ❌ |

Reliability is 1.0 on every workload and seed. What we watch in each run: the
three graded metrics, per-round `slowdown` in `results.json`, queue drops
(`qdrops` in the sweep), and the per-flow lines the sender logs at each round
start and at the end (rate, carried rate, cuts, retransmissions).

## 9. Risks and open questions

- **Hardcoding the topology.** We assume the operator knows its own backbone,
  which is reasonable for a datacenter. If the graded workloads are far from
  uniform all-to-all, the starting rate is wrong for about one RTT until
  feedback lowers it.
- **Unused capacity.** The rate never goes above the topology share, so when
  fewer servers are sending than all-to-all assumes, we leave capacity unused.
  Using it would need a signal for spare capacity, which an empty queue can't
  give.
- **Random loss.** See §5.4: 1% passes; 5% on a single link is just over the
  bound, and 5–10% on a spine link fails it (many rounds still need two round
  trips). Getting further would take parity packets. Whether that matters
  depends on the graded workloads.
- **No in-flight cap.** During a blackout, new data is still paced out into
  the dead link, which costs overhead (server_failure: 1.12, under budget).
- **Python timer cost.** One pacer tick per datagram; the full sweep (13
  workloads × 5 seeds) takes about 25 s of wall time, so this is fine.

## 10. Division of work

As in the design doc: both of us build M1 together (codec, receiver, test
sweep script). After that:

- **Eric**: pacer, topology prior, delay-based rate control (the
  replacement for the hop-record telemetry), and the congestion test workloads
  (medium/large incast, slow spine, staggered starts).
- **Ellis**: SACK, loss detection, PTO/backoff, ack policy (immediate vs
  every-2), overhead tuning, and the loss/failure/reordering workloads.

Then we split investigation based on what the sweep shows is the worst
metric.

---

## Course setup and running

*(From the starter repo.)*

Design a transport protocol for one of three scenarios, simulated with ns-3.
The full project spec is on the course website (cs168.io).

### Setup

You need git, Python 3.10 or newer, a C++ compiler, CMake 3.25 or newer,
and [uv](https://docs.astral.sh/uv/). The project spec on the course website
has the full per-platform instructions; in short:

    # macOS
    xcode-select --install
    brew install cmake python uv

    # Ubuntu, or Windows inside WSL2
    sudo apt install git build-essential cmake python3-dev

Install the Python packages in a virtual environment inside the repo, using
[uv](https://docs.astral.sh/uv/) (`brew install uv`, or
`curl -LsSf https://astral.sh/uv/install.sh | sh`):

    uv venv
    source .venv/bin/activate
    uv pip install pyyaml matplotlib pytest
    uv pip install cmake      # Ubuntu 22.04 only: its packaged CMake is too old

Run `source .venv/bin/activate` again in every new terminal before using
`make`. Then:

    make setup        # once: downloads and builds ns-3, a few minutes
    make doctor       # every check should pass

#### Using the instructional machines

If you'd rather not set up your own computer, there is a course VM with
everything above already installed. Log in to an instructional machine (for
example `ashby.eecs.berkeley.edu`) and start it:

    qemu-cs168.sh     # leave this running; Ctrl-C shuts the VM down

Then log in to the VM from a second terminal on the same machine:

    ssh-cs168-vm

Work in `~/vmfiles` inside the VM because it is the only folder that survives a
shutdown; everything else is wiped. Clone your repo there, `cd` into it, and run
`make setup` and `make doctor`. Skip the venv and `uv pip install`: the VM already
has the Python packages, and ns-3 is prebuilt, so `make setup` takes about a
minute.

Outside the VM, the same files will be in `~/proj3-vm/vmfiles` on your
instructional account. Copy runs from there to your own computer to open them
in Wireshark or a browser. Copies of both scripts are in `tools/`.

### Running

    make run SCENARIO=datacenter                                   # runs protocol.py
    make run SCENARIO=datacenter WORKLOAD=workload.yaml            # your own workload
    make run SCENARIO=datacenter PROTOCOL=example/baseline_stopwait
    make run SCENARIO=datacenter PCAP=1 VIS=1                      # Wireshark pcaps + vis.html

SCENARIO is `leo`, `datacenter` or `sensor`. Results are in `runs/`.
`make help` lists everything.

### What's in the repo

| path | what it is |
|---|---|
| `protocol.py` | our protocol, the one file we submit. `SCENARIO = "datacenter"` at the top |
| `workload.yaml` | template for our own test workloads |
| `example/` | a stop and wait baseline protocol |
| `scenarios/` | the three network scenarios, each with a basic workload |
| `transportlab/` | the harness. `api.py` is the only file with commands we need |
| `sim/`, `third_party/` | the ns-3 backend that `make setup` builds |
