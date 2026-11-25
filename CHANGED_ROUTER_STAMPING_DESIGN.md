# Design Change: Endpoints Measure Congestion Instead of Switches

Ellis O'Dowd, Eric Khodorenko

**Summary:** switches can't write anything into our packets, so we can't use
HPCC's hop records. Instead, every packet carries a send timestamp. All hosts
share one clock, so the receiver can see exactly how long each packet waited
in queues, and it reports that back in the ACK. The sender slows down when
queueing goes up and speeds up when it's near zero. The idea is still HPCC's:
learn the load from every packet and react before anything is dropped. Only the
source of the information has changed.

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

The receiver puts this number in every ACK. The sender reacts in two ways:

- **Queueing is low (near zero):** there's room, so send faster.
- **Queueing rises past a small target:** slow down, and cut harder the further
  over the target it is.

That's the same shape as HPCC's reaction, and it acts before queues overflow,
not after packets are lost.

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
tell them apart. We make up for it in two ways:

- **Start at a sensible rate.** We know the topology and every link's speed
  (from the scenario file), so each sender starts each flow at its fair share
  of the busiest link, instead of guessing or using slow start. This is
  important because most rounds are short enough to finish before the first
  feedback arrives.
- **Probe upward quickly.** If queueing stays at zero and we still have data to
  send, we raise the rate fast until queues just start to form.

**Feedback is one round trip old.** That's also true of HPCC, which is another
reason the starting rate matters so much.

## Is this a real approach?

Yes. Google's Swift congestion control runs in production datacenters using
only delay measured at the endpoints, with no help from switches. TIMELY (also
Google) reacts to how fast round-trip time is changing, and TCP Vegas and BBR
are older examples of delay-based control.

We have it easier than any of them, because the simulator's shared clock gives
us exact one-way delay. They have to estimate it from round-trip times, which
mixes in delays on the ACK's return path.

## What changes in the packet

- **Removed:** the hop count, the hop records, and the ACK's echoed copies of
  them.
- **Added to data packets:** a 4-byte send timestamp.
- **Added to ACKs:** the echoed send timestamp (so the sender can measure round
  trip time) and the one-way delay the receiver measured.

The result is smaller than the original design. A packet crossing the spine
would have picked up three hop records, 33 bytes in all. The timestamps cost a
few bytes. The full header layout is in
[CHANGED_UDP_HEADER_DESIGN.md](CHANGED_UDP_HEADER_DESIGN.md).
