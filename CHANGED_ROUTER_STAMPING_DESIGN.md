# Design Change: Endpoints Measure Congestion Instead of Switches

Ellis O'Dowd, Eric Khodorenko

**Summary:** switches can't write anything into our packets, so we can't use
HPCC's hop records. Instead, every packet carries a send timestamp. All hosts
share one clock, so the receiver can see exactly how long each packet waited
in queues, and it reports that back in the ACK. The sender cuts its rate when
queueing builds, climbs back when it clears, and remembers a slow link from one
round to the next. The idea is still HPCC's: learn the load from every packet
and react before anything is dropped. Only the source of the information has
changed.

The implementation details are in [README.md](README.md).

---

## What we originally planned

Our design document borrowed from HPCC. As a data packet crossed the network,
each switch would attach a small "hop record": how full its queue was, how
much it had sent, and how fast its outgoing link was. The receiver would copy
those records into its ACK, and the sender would look at the busiest switch on
the path and set its sending rate to keep that switch just below full.

It's an appealing design because the sender gets an exact, per-switch picture
of the network on every round trip.

## The problem

HPCC depends on switches that are programmed to write those records. In this
project, the switches are ordinary routers. They forward packets or drop them
when their queue is full, and they never read or change what's inside.

The only code we control is `protocol.py`, and it runs only on the servers at
each end. Grading uses the course's simulator, so we can't change how the
switches behave either.

So hop-record fields in our header would arrive at the receiver exactly as the
sender wrote them. They'd cost bytes and carry no information.

## The fix: measure queueing from the ends

The key fact is that **every host in the simulator reads the same clock.** If
the sender writes "sent at time T" into a packet, the receiver can subtract T
from its own clock and get the exact one-way delay.

That delay has two parts:

- **The fixed travel time** of the path. We know it from the topology (40 ms
  within a rack, 50 ms across racks), and we can also learn it as the smallest
  delay we've seen.
- **Time spent waiting in queues.** That's everything above the fixed part.

So `queueing delay = measured delay − fixed delay`, and it tells us how backed
up the path is. Packets leave a queue one at a time at a known speed, so a
queueing delay converts directly into roughly how many packets were waiting
ahead of ours. That's very close to the "queue length" HPCC would have read
from a hop record.

The receiver puts this number in every ACK, and the sender reacts:

- **Queueing rises past a small target** (about half a switch queue): cut the
  rate, harder the further over target it is, at most once per round trip.
- **Queueing is back near zero and there's still data to send:** climb back
  up, slowly, but never above the flow's fair share from the topology (see
  [CHANGED_SLOW_START_DESIGN.md](CHANGED_SLOW_START_DESIGN.md)).

That's the same shape as HPCC's reaction, and it acts before queues overflow,
not after packets are lost. Climbing back is deliberately cautious: by the
time an ACK reports a queue, the network is a full round trip further along,
and the switch queues are tiny. Any overshoot overflows a queue before the
sender can hear about it.

## Telling a real queue from noise

Delay can rise for reasons that aren't congestion. Our tests include links
with random delay "jitter", where each packet is held up by a random amount.
Our first version cut the rate whenever any single packet looked delayed, and
jitter alone collapsed the rates to almost nothing.

The fix is to look at the *smallest* queueing delay among the last few
packets. Jitter delays packets at random, so at least one of a handful usually
gets through quickly and the minimum stays low. A real queue delays *every*
packet behind it, so the minimum rises with it. (The Copa congestion control
uses the same filter.)

## Remembering a slow link

Most rounds finish sending before the first ACK comes back, so a round can't
fix its own rate in time. If a link is slow for good, every round would start
too fast and overflow it again. So the sender carries what it learned into the
next round:

- If a round both **cut the rate** and **lost packets** (a real overflow, not
  jitter, which never drops anything), the next round starts at the rate
  the bottleneck actually delivered this flow's packets.
- That rate comes from the ACKs: while a queue is backed up, it releases this
  flow's packets at exactly the flow's share of the slow link, so the ACKs
  come back at that rate. This is the same quantity HPCC reads from its
  `txBytes` field, measured from the ends.
- The carried rate creeps back up only after a few clean rounds in a row,
  because a link that's exactly full looks the same as one with room to spare.

## But how do we know *where* the congestion is?

We don't, exactly. The delay we measure is the total for the whole path, not
broken down per switch. In practice that matters less than it sounds:

- **Usually only one link is congested at a time.** If the total queueing is
  high, it's almost all at that link, and the right reaction is the same
  wherever it is: slow down.
- **We can often work it out anyway.** Each sender has 8 flows on different
  paths. If only its flows to other racks show queueing, the problem is the
  link from its rack up to the spine. If only flows into one rack are slow,
  it's the spine's link down into that rack. If only one receiver is slow, it's
  that receiver's link. The topology is small, so the pattern points to the
  link.
- **The receiver has its own view.** If every sender's packets arrive delayed by
  the same amount, the receiver's own incoming link is the bottleneck.

## What we lose, and how we make up for it

**We can't see spare capacity.** A link that's half used and a link that's
exactly full both have empty queues, so they look the same to us. HPCC could
tell them apart. We handle this by **starting at a sensible rate**: we know
the topology and every link's speed (from the scenario file), so each flow
starts at its fair share of the busiest link instead of guessing or using slow
start. The cost: we never go above that share, so when fewer servers are
sending than expected, some capacity goes unused. That's the cheaper mistake,
since going too fast costs drops and retransmissions.

**Feedback is one round trip old.** That's also true of HPCC, and it's why the
starting rate and the carried-over rate matter so much.

## Is this a real approach?

Yes. Google's Swift congestion control runs in production datacenters using
only delay measured at the endpoints, with no help from switches. TIMELY (also
Google) reacts to how fast round-trip time is changing, and TCP Vegas and BBR
are older examples of delay-based control.

We have it easier than any of them, because the simulator's shared clock gives
us exact one-way delay. They have to estimate it from round-trip times, which
mixes in delays on the ACK's return path.

## What changes in the packet

The hop count, hop records, and the ACK's echoed copies are gone. Data packets
gain a 4-byte send timestamp; ACKs gain the echoed timestamp (for round-trip
time) and the one-way delay the receiver measured. That's a few bytes, against
the 33 bytes of hop records a packet crossing the spine would have collected.
The full layout is in [CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md).
