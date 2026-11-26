# Design Change: No Slow Start; Pace from a Known Starting Rate

Ellis O'Dowd, Eric Khodorenko

**Summary:** our design document said we'd use slow start and then ramp up
quickly using the queue information from each packet. In this network that
doesn't work: most rounds are over before the first feedback comes back, so
whatever rate we start at is effectively the rate for the whole round. Instead,
each sender works out its fair share of the network from the known topology,
starts at that rate from the very first packet, and spaces its packets out
evenly ("pacing") instead of sending them in bursts. Delay feedback then
corrects the rate when the starting guess turns out to be wrong.

The implementation details are in [README.md](README.md).

---

## What we originally planned

Tradeoff #4 in the design doc: start with a slow start, like TCP, but increase
quickly because the hop records tell us exactly how loaded the queues are.
Slow start means sending a small amount, waiting for ACKs, then doubling, so
the sender feels its way up to the right rate over several round trips.

## The problem: the round is over before the feedback arrives

Slow start learns the right rate one round trip at a time. That only helps if
a transfer lasts many round trips. Ours usually don't.

Each round, every server sends a fairly small amount to each of the others.
At full speed, a server gets its whole share of a round onto the wire in less
than one round trip, unless the amount per destination is quite large. So by
the time the first ACK arrives with any information about the network, the
sender has already finished sending. Slow start would still be in its first
small step when the round should be over, and we'd spend several round trips
creeping up to speed on a round whose ideal time is about half of one.

## The opposite mistake: just send everything at once

If slow start is too cautious, why not send everything immediately at full
speed? Because the switch queues here are tiny compared with how much a sender
can have in flight.

- A server can put hundreds of packets on the wire before its first ACK
  returns, but each switch queue only holds 64 packets.
- Three servers share each rack's link to the spine, and that link carries
  only half their combined speed. (The spine is the switch connecting the
  racks; see the README for the topology.) If all three send to other racks at
  full speed, that link gets twice what it can carry.

So blasting a medium-sized round overflows the rack-to-spine queues within a
few milliseconds. Every dropped packet has to be retransmitted, which costs
overhead, and if it's one of the last packets of the round, it adds a whole
round trip to the round's time.

So we can't creep up slowly and we can't blast. We need to send at about the
right rate from the first packet, with no feedback to tell us what that rate
is.

## The fix: compute the starting rate from the topology

We know the network's layout and every link's speed from the scenario file
(`scenarios/datacenter/scenario.yaml`). Our code isn't told any of this while
it runs; we build it in as constants, and each server works out its rack from
its own name (server N is in rack N ÷ 3).

In a round, every server sends to every other server, so a sender can assume
everyone else is doing the same thing it is. From that, it works out a fair
share for each of its flows:

- **Flows to other racks** have to cross the busy rack-to-spine links, so
  they get an even slice of those links.
- **Flows within the rack** never touch the spine, so they get the rest of
  the sender's own link.

With every sender using these shares, the busiest links run full but not
over, and no queue builds up. On an ordinary round, that alone comes within a
few percent of the best possible time.

**Leaving room for ACKs.** Each rack's link to the spine carries more than
its outgoing data: it also carries the ACKs for all the data coming *into*
that rack, about 3% extra. Our first version filled the link 100% with data,
and on long rounds that extra 3% slowly built a queue until it overflowed. So
the cross-rack share leaves room for the ACKs.

## Pacing: spreading packets out

Knowing the right rate isn't enough if packets still leave in bursts. A burst
of packets arriving at a switch at once fills its queue even when the
average rate is fine. So the sender uses a timer to release packets evenly
spaced at its chosen rate, instead of dumping a whole window at once. That
keeps the queues near empty.

Two details matter. Retransmissions go out before new data, because a round
only ends when its last message is complete, and the message with the missing
piece can't complete until its retransmission arrives. And the sender never
hands its own network card more than it can send right away, so a
retransmission can't get stuck behind a backlog there.

## Is it OK to build the topology in?

We think so:

- **The project is to design for one scenario.** Grading runs on this exact
  network. Using what we know about it is the point of a scenario-specific
  design.
- **Datacenter operators know their own network.** They built it and
  configured every link. Real datacenter protocols are tuned with link speeds
  and round-trip times.
- **Our original design already told the sender link speeds.** The hop
  records had a "link bandwidth" field written by each switch. Since switches
  can't write it, the scenario file stands in for that field.
- **We can't discover it in time.** We could measure the topology (rack
  membership shows up in delay, link speed in how far apart packets arrive),
  but only after a round trip, and by then the round is mostly over.

## When the starting guess is wrong

The calculation assumes every server is sending a full round to every other
one over a healthy network. That isn't always true: a link can slow down, a
server can fail, senders can start at slightly different times, or the
traffic can be uneven. This is where the endpoint delay measurements from
[CHANGED_ROUTER_STAMPING_DESIGN.md](CHANGED_ROUTER_STAMPING_DESIGN.md) come
in:

- **Starting rate too high** (say a link slowed down): queueing delay rises,
  and the sender cuts its rate. A short round may be over before that helps,
  so if the round also lost packets, the next round starts at the rate the
  slow link actually delivered.
- **Starting rate too low** (say fewer servers are sending): we stay at the
  fair share and don't try to go faster. An empty queue can't tell us whether
  there's room, and in this network an overshoot overflows a queue before
  the feedback arrives.

Starting too low is the cheaper mistake: it costs a little time, while
starting too high costs drops and retransmissions. So the rate never goes
above the fair share.

## Real-world precedent

Datacenter transports generally skip slow start for the same reason: transfers
are short compared with how fast the network is. HPCC itself starts new flows
at full link speed and relies on its feedback to rein them in, and Homa lets
each message send its first round trip's worth of data immediately. Our
version is more careful about that first round trip because our feedback is
slower to arrive than HPCC's, and because the spine links are shared by more
senders than they can carry at full speed.
