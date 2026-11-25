# CS168 Project 3: A Transport Protocol for the Data Center Backbone

Ellis O'Dowd, Eric Khodorenko

This README is our implementation plan. It starts from our design document
(an HPCC-style protocol with SACK and immediate acks) and changes it where the
simulator, the topology, or the grading math call for it. Each change is
explained in [section 3](#3-what-changes-from-the-design-doc-and-why).

Short, high-level explanations of the two biggest changes:

- [CHANGED_ROUTER_STAMPING_DESIGN.md](CHANGED_ROUTER_STAMPING_DESIGN.md): why
  switches can't stamp hop records, and how endpoints measure congestion instead
- [CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md): why our header
  sits inside UDP, which fields we dropped and added
- [CHANGED_HANDSHAKE_DESIGN.md](CHANGED_HANDSHAKE_DESIGN.md): why there's no
  SYN/FIN handshake, and what replaces it
- [CHANGED_SLOW_START_DESIGN.md](CHANGED_SLOW_START_DESIGN.md): why slow start
  doesn't work here, and how we pick and pace the starting rate from the
  topology

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

| Design doc | Plan | Why |
|---|---|---|
| Switches stamp hop records (switch ID, qlen, txBytes, BW) into packets, HPCC-style | **Endpoints measure one-way queueing delay** with the shared clock, and the receiver echoes it in acks | ns-3 switches here are plain drop-tail routers (`sim/tlsim.cc`). Only `protocol.py` runs, and only on hosts, so nothing can write hop records en route. Every host shares one clock (`self.now()` is "the same clock on every host"), so `receiver_now − send_ts` is the exact forward one-way delay. Subtracting the base delay gives the total queueing on the path, which serves as our telemetry. |
| SYN / SYN-ACK / FIN handshake (timing diagram) | **0-RTT, connectionless.** The first packet of a flow carries data. No FIN. | A handshake adds 100 ms to a 55 ms ideal, which alone is a slowdown of about 2.8 (fact 1). Flow state is created when the first packet arrives. Every message ID is globally unique, so we don't need connection setup to tell flows apart. |
| Slow start, then ramp quickly | **Start at a topology-aware fair-share rate, paced from the first packet**, then adjust with delay feedback | Fact 3: most rounds are over before slow start would get going. Section 5.3 has the details. |
| Byte-offset sequence numbers + cumulative ACK + SACK | **Kept** | Works as designed. Retransmissions reuse their byte range and carry a fresh send timestamp, so delay samples stay unambiguous. |
| Source/dest ports, checksum | **Dropped** | The harness only sends UDP, and UDP's header already carries ports and a checksum covering our whole header and payload. See [CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md). |
| Receive window | **Kept, in ACKs only** | Never binding in the simulator, but a real receiver needs flow control. |
| Up to 15 SACK blocks × 8 B | **Same format; the receiver includes at most `MAX_SACK` (≈ 4)** | We ACK every packet, so older holes were already reported in earlier ACKs. Fewer blocks keep ACKs small during loss. |
| One header for data and ACKs | **Separate data and ACK headers** sharing the flags byte | Senders and receivers are separate endpoints, so data packets never carry ACK info and ACKs never carry data. |
| Immediate ack for every packet | **Kept for v1.** Measure afterwards, and possibly move to ack-every-2 plus immediate acks on gaps and message ends | Costs about 3% overhead (section 6). We have the budget, and fresh delay samples help congestion control. |
| One message per packet (implied) | **Each packet carries `msg_id` + offset in message + END flag** | Large messages are split across datagrams (1472 B limit), and a message is delivered as soon as all its pieces arrive, even if an earlier packet is missing. Packing several small messages into one datagram is a possible later extension (section 6). |

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
| `END` | data: this packet holds the last byte of its message |
| `PROBE` | data: sent by the tail-loss/RTO timer, so the receiver acks immediately (it always does anyway) |
| (spare) | reserved, 0 |

### 4.1 Data packet: 16-byte header

| Field | Bytes | Description |
|---|---|---|
| flags \| #SACK | 1 | `ACK` = 0; #SACK = 0 |
| sequence number | 4 | byte offset of this payload in the sender→receiver stream (as in the design doc) |
| send timestamp | 4 | µs, when the sender called `send_datagram`. A retransmission gets a fresh timestamp |
| msg_id | 4 | the harness's message ID (globally unique) |
| offset in message | 3 | where this payload goes in the message (messages ≤ 16 MB) |

Payload length = datagram length − 16. When the `END` packet arrives, the
receiver knows the message's total length: `offset + payload length`. Room
for 1456 bytes of payload, so a 1400-byte message fits in one datagram.

Each flow's byte stream is its messages laid end to end. A packet never spans
two messages: when a message's last piece is smaller than a full datagram, the
next message starts in a new packet.

### 4.2 ACK packet: 14 bytes + 8 per SACK block

| Field | Bytes | Description |
|---|---|---|
| flags \| #SACK | 1 | `ACK` = 1; #SACK = number of blocks that follow (0 to `MAX_SACK`) |
| cumulative ACK | 4 | next byte expected; every byte before it has arrived |
| receive window | 2 | free buffer space in 256-byte units (as in the design doc) |
| echo timestamp | 4 | send timestamp of the data packet that triggered this ACK, for RTT |
| forward OWD | 3 | µs, `receiver.now() − send_ts` for that packet. **Replaces the echoed hop records** |
| SACK block × n | 8 | `(left, right)` byte offsets of a received range above the cumulative ACK, newest first |

The usual ACK is 14 B, or 42 B on the wire. The doc's version was
18 + 8k + 33 bytes of echoed hop records.

## 5. Endpoint behavior

### 5.1 Flows and state

- A **flow** is one (sender endpoint → receiver) pair. Each sender has at most
  8 flows.
- Sender state per flow: next stream byte (sequence number); queue of messages
  not yet sent; sent-and-unacked segments `seq → (msg_id, offset, len,
  send_time)`; SACK scoreboard (byte ranges the receiver has reported); rate;
  base OWD (minimum seen); srtt/rttvar; loss/probe timer deadline.
- Receiver state per source address: received stream bytes as a range set
  (gives the cumulative ACK and the SACK blocks); per message, a `bytearray`
  plus received byte intervals; the set of completed `msg_id`s (also checked
  with `self.delivered(msg_id)`).

### 5.2 Sending: gather, packetize, pace

1. `on_app_data` appends the message to its flow's queue and sets a
   **0-delay "flush" timer**. All messages of a round are offered at the same
   simulated instant, so this lets us see the whole round before choosing a
   send order. If we sent on each call instead, whichever destination came first
   in offer order would get an unfair head start.
2. The **packer** turns each flow's queue into datagrams of up to 1472 B,
   assigning each the next range of stream sequence numbers. Large messages
   are split into multiple packets; a packet never spans two messages.
3. The **pacer** is one timer per sender. At each tick it sends the next
   datagram from the flow that is furthest behind its rate (virtual-time
   fair queuing over per-flow rates). Retransmissions jump the queue. The tick
   interval is `wire_bytes / aggregate_rate`, capped at NIC line rate.
4. Inflight cap per flow: `rate × (base_RTT + 2·target_delay)`. This stops a
   flow from dumping its backlog during a blackout.

### 5.3 Rate control (our endpoint-only HPCC)

**Initial rate (the prior).** We know the topology, so the first RTT is paced
at the share each flow would get if every server were doing all-to-all:

- cross-rack: the rack uplink (150 Mbps) is shared by 3 senders → 50 Mbps per
  sender → split over its active remote flows (≈ 8.3 Mbps each for 6)
- intra-rack: the rest of the NIC (50 Mbps) → split over its active local flows
  (25 Mbps each for 2)

Check: every receiver then gets 6 × 8.3 + 2 × 25 = 100 Mbps, exactly its
downlink. Every rack↔spine link carries exactly 150 Mbps. **The prior alone
should land within a few percent of the ideal on uniform all-to-all rounds.**
The rack of a host is `index // 3`, read from the peer name.

**Feedback (corrects the prior when it's wrong).** Cases: bandwidth changes,
failed servers, staggered starts (`jitter_s`), uneven patterns. Per flow, on
every ACK:

```
q   = fwd_owd − base_owd          # queueing on the forward path (sum over hops)
T   = target queueing delay        # start ~1.5 ms (≈ 20 pkts at 150 Mbps; queues hold 64)

if q < T:      rate += AI_STEP · (acked_bytes / rate·RTT)          # additive, ~+1 Mbps per RTT
               if q ≈ 0 for a whole RTT and backlog remains: rate *= 1.25   # fast probe up
if q > T:      at most once per RTT: rate *= max(0.5, 1 − β·(q − T)/q)      # Swift-style MD
on loss:       at most once per RTT: rate *= 0.7
clamp rate to [0.5 Mbps, 100 Mbps]
```

This works like HPCC: a multiplicative cut that grows with how far over target
the path is, reacting to queue buildup well before any drop. HPCC reads per-hop
qlen/txBytes; we read the total path queueing delay. We usually have a single
bottleneck, so the two signals carry nearly the same information.

`base_owd` is the windowed minimum (last ~5 s) of the forward OWD, starting from
the topology values (40 / 50 ms). The window lets the protocol follow
`delay_change` events.

**Across rounds.** Rounds are seconds apart. At the start of a round, each flow
goes back to the prior, blended toward the rate it ended the last round at if
that round saw sustained congestion (a lasting `bw_change`, for example).

### 5.4 Loss recovery

- **SACK-based fast retransmit.** A segment is declared lost once at least 3
  segments sent after it are covered by the cumulative ACK or SACK blocks, or
  once a later segment is acked and this one is more than `9/8 · srtt` old
  (QUIC's time threshold, which tolerates jitter reordering). It is resent
  ahead of new data with the **same sequence number and a fresh send
  timestamp**, so delay samples from retransmissions stay accurate.
- **Tail loss probe / RTO.** The last packets of a round have nothing after
  them to trigger SACK detection, and the tail is exactly what step time
  measures. The probe timer fires at
  `PTO = srtt + max(4·rttvar, 2 ms) + target_delay`, which is about 1.1 × RTT
  (≈ 110 ms in sim, ≈ 1.1 ms real). TCP's 200 ms+ minimum RTO would be about
  200 RTTs in real units. On a PTO, resend the **newest** unacked segment with
  the `PROBE` flag. Its ACK's cumulative ack and SACK blocks show exactly which
  earlier segments are missing.
- **Backoff during outages.** Consecutive PTOs double, capped at 4 × RTT.
  During a blackout, each flow sends at most one probe per backoff interval, so
  a dead server costs a few probes, not a window of retransmits. As soon as an
  ack arrives, the timer resets to its normal value and the rate returns to the
  prior.
- **Optional, only if tests show we need it: tail redundancy.** If the flow has
  seen random loss recently (a `loss_burst`), send the last datagram of each
  flow's round twice. Duplicating a tail packet with 4 packets per pair costs
  about 25% overhead, so this is only turned on after loss has been observed.

### 5.5 Receiver

On every data packet:

1. Add `[seq, seq + len)` to the flow's received range set. If the message
   isn't already complete, copy the payload into that message's buffer at
   `offset`.
2. When the message's received intervals cover `[0, total)` (total known once
   the `END` packet arrives), call `self.deliver(msg_id, bytes)`, mark it done,
   and free the buffer. This doesn't wait for earlier messages in the stream.
3. **Always ACK**, including for duplicates. A lost ACK must never wedge the
   sender. The ACK carries the cumulative ack (end of the first contiguous
   range), receive window, up to `MAX_SACK` blocks for ranges above it (newest
   first), echo timestamp, and forward OWD.

## 6. Overhead budget

1400-byte messages (the basic workload), 1 message per datagram, 1 ACK per
datagram:

| Item | Wire bytes per message | Ratio contribution |
|---|---|---|
| payload | 1400 | 1.000 |
| IP/UDP | 28 | 0.020 |
| our data header | 16 | 0.011 |
| ACK (28 + 14, no SACK blocks) | 42 | 0.030 |
| **Total, no loss** | **1486** | **≈ 1.061** |
| Budget | 1820 | 1.30 |

That leaves about **330 B per message, or ~22% of packets, for
retransmissions and probes**. Moving to ack-every-2 would bring the no-loss
figure to about 1.05.

**Small messages are the risk.** A 100-byte message sent alone costs
100 + 16 + 28 + 42 = 186 B, a ratio of 1.86, which fails. If the graded
workloads use small messages, we'd add a packet format that packs several
messages to the same receiver into one datagram (a per-message
`msg_id`/offset/length entry each), plus ack-every-2. That would bring the
ratio to roughly 1.15. Not planned for v1; the basic workload uses 1400-byte
messages.

## 7. Implementation plan

The course only accepts `protocol.py`, so all of the code lives in that one
file, organized into sections:

1. **Constants / topology**: `RACK_SIZE = 3`, link rates, target delay, PTO
   floor, `rack_of(name)`.
2. **Codec**: `struct` definitions and `pack_data`, `unpack_data`, `pack_ack`,
   `unpack_ack`. Unit tests round-trip them.
3. **`RangeSet`**: sorted disjoint intervals with add, contains, and
   `gaps_above(x)`. Used for received stream bytes, SACK, and message byte
   coverage.
4. **`FlowTx`**: per-flow sender state, packer, loss detection, and the rate
   controller.
5. **`MySender`**: flush timer, pacer timer, probe timer (one timer for the
   earliest flow deadline), and ACK handling.
6. **`MyReceiver`**: reassembly, delivery, ACK generation.

Milestones (each must keep reliability = 1.0 on everything that passed
before):

| # | Milestone | Done when |
|---|---|---|
| M1 | Codec + framing + 0-RTT send-all + receiver reassembly + per-packet ACK + RTO retransmit | basic_workload: reliability 1.0. Slowdown is probably already < 2.5 (no handshake; 32 pkts per sender fit in the queues) |
| M2 | Pacer with topology prior | medium/large bursts: zero drops at rack uplinks (check `sim-stats.json` / `VIS=1`) |
| M3 | SACK + fast retransmit + PTO + backoff | loss_burst and blackout workloads: reliability 1.0, overhead < 1.3 |
| M4 | Delay-based feedback | bw_change and jitter_s workloads stay < 2.5 |
| M5 | Overhead tuning (ack-every-2, SACK cap, tail redundancy only if needed) and parameter sweep | full test matrix passes, worst of 5 seeds |

## 8. Test plan

Custom workloads go in `workloads/` (copied from `workload.yaml`) and run with
`make run SCENARIO=datacenter WORKLOAD=workloads/<name>.yaml SEED=n`. A small
script (`tools/sweep.py`) runs every workload with seeds 1–5 and prints the
**worst** value of each metric, which is how grading works.

| Workload | Setup | What it checks | Ideal (approx.) |
|---|---|---|---|
| basic | given: 2 rounds, 5.6 KB/pair | correctness, 0-RTT | 55 ms |
| medium incast | `burst_bytes: 28000`, 5 rounds | pacing in the first RTT (would overflow queues unpaced) | 77 ms |
| large incast | `burst_bytes: 140000`, 5 rounds | multi-RTT; feedback loop; no slow-start penalty | 187 ms |
| small msgs | `msg_bytes: 100` | overhead with small messages (decides whether we need packing) | – |
| large msgs | `msg_bytes: 64000` | fragmentation / reassembly | – |
| random loss | + `loss_burst` 1%, 5%, 10% on `rack0-spine` during rounds | SACK, PTO, overhead under retransmits | – |
| server failure | + `blackout` on `sender4-rack1` and `rack1-receiver4` for 1 s | backoff, recovery, no wasted overhead | ideal waits for recovery |
| slow spine | + `bw_change` `rack1-spine` → 75 Mbps | feedback corrects a wrong prior | rises with the change |
| jitter / reordering | + `delay_change` with `jitter_ms: 5` | reorder tolerance (no spurious retransmits) | – |
| staggered starts | incast with `jitter_s: 0.02` | prior isn't stuck assuming everyone is active | – |

What we watch in each run: the three graded metrics, per-round `slowdown` and
`undelivered` in `results.json`, queue drops in `sim-stats.json`, queue depth
over time in `VIS=1`, and retransmit counts from our own `self.log`.

## 9. Risks and open questions

- **Hardcoding the topology prior.** We assume the operator knows its own
  backbone, which is reasonable for a datacenter. If the graded workloads are far
  from uniform all-to-all, the prior is wrong for about 1 RTT until feedback
  fixes it. Fallback: start every flow at `NIC / active_flows`, which is
  safe for the receiver downlinks but 1.5× too fast for the spine, and rely on
  feedback.
- **Tail latency on small rounds.** With a 55 ms ideal, any retransmission of a
  round's last packet costs more than 2.5× for that round. Only the mean has to
  pass, but a long `loss_burst` across many small rounds could still fail it.
  That's what the optional tail redundancy is for.
- **ACKs share the bottleneck.** ACKs for cross-rack data cross the
  rack→spine links in the opposite direction from the data, and those same
  links carry other traffic. They take about 3% of capacity and can be dropped
  at the 64-packet queue. Ack-every-2 helps here too.
- **Python timer cost.** With one pacer tick per datagram, large workloads
  mean tens of thousands of callbacks. That's fine in simulated time, but we
  should check wall-clock run time. Sending a small batch per tick is a cheap fix.

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
