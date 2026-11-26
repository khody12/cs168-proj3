# Design Change: Recovering From Loss Without Losing Whole Rounds

Ellis O'Dowd, Eric Khodorenko

**Summary:** our design document planned SACK, an ACK for every packet, and
a short retransmit timer. We kept all three, but in this network that alone
isn't enough: a single lost packet makes its round take more than twice the
ideal, and a packet lost *twice* makes it take about five times. So we added
three things: detect losses the moment an ACK proves them, watch each flow's
last packet with its own timer, and send two copies of a retransmission when
the loss looks random. Nothing here costs anything on a network that isn't
losing packets.

The implementation details are in [README.md](README.md) §5.4.

---

## What we originally planned

Tradeoffs #2, #3 and #5 in the design doc: SACK so the receiver can say
exactly which packets it has, an immediate ACK for every packet, and a
retransmit timer much shorter than TCP's, because round trips inside a
datacenter are tiny.

## The problem: one round trip is already too long

The best possible time for a small round is about *half* a round trip: the
data just has to travel one way. A lost packet can't be resent until the
sender learns it's missing, which takes about a full round trip, and the
resend then has to travel one way again. So a round that loses even one
packet takes roughly 2.3 times its ideal, just under the 2.5 limit.

That leaves no room for a second mistake. If the retransmission is lost too,
or the loss is noticed late, the round takes another round trip and lands
around 5 times its ideal. Only the average over rounds is graded, but one
such round in a short run is enough to fail it.

## Detect losses as soon as an ACK proves them

All of a flow's packets take the same path, and the switches send packets
out in the order they arrived. So packets in one flow can't overtake each
other: if a *later* packet has been acknowledged and an earlier one hasn't,
the earlier one is gone. We resend it the moment that ACK arrives.

Some workloads add random delay "jitter", which *can* reorder packets. When
we see that happen (an older packet acknowledged after a newer one, or a
retransmission acknowledged too fast to have been the retransmission), we
start waiting a little before declaring a loss. This is the standard RACK
approach from modern TCP.

## Watch the last packet

The last packet of a flow is special: nothing comes after it, so no later ACK
can reveal that it was lost. Only a timer can. Our timer used to count from
the flow's most recent send of *anything*, so resending an earlier lost
packet pushed the last packet's deadline back a full round trip. Now the timer
counts from when the last packet itself was sent.

When it fires, everything still unacknowledged is overdue, since it was all
sent before the last packet. If that's only a few packets, we resend all of
them at once instead of resending one and spending another round trip finding
the next.

## Two copies when the loss looks random

A retransmission that gets lost costs a second round trip. Sending it twice
means both copies have to be lost, which at 1% loss is 1 in 10,000 instead of
1 in 100. Only retransmissions are doubled, so a healthy network pays nothing,
and the cost grows with the loss rate.

Doubling is the wrong move in two situations, and we can tell them apart:

- **The loss was congestion.** If the path had a standing queue when the
  loss was detected, the packet was dropped by an overflowing queue. An extra
  copy would add load to exactly the link that's overloaded, so we send one.
- **Many packets were lost at once.** Random loss is scattered. A burst of
  losses in one flow means an outage, like a failed server coming back. A
  dead link has no queue either, so the first check can't catch it. Doubling
  a whole burst floods the path as it recovers, so we send one copy each.

In the same spirit, repeated probes into a path that has gone silent aren't
doubled either.

Our first version doubled everything. It helped on random loss but made the
slow-link and server-failure tests worse, for exactly these two reasons.
Adding the checks kept the gains and removed the damage.

## What we didn't build

Parity packets (forward error correction) could rebuild a lost packet with no
round trip at all, and would help at 5–10% random loss. We left them out.
Real datacenter loss is almost all congestion, which our pacing and delay
feedback prevent. Links already correct bit errors in hardware, and a link
that starts losing packets is routed around. Building heavy redundancy into
the transport would be tuning for our own tests rather than for a datacenter.

## Real-world precedent

The detection rule is RACK (RFC 8985), used by Linux TCP and QUIC. Watching
the tail with a probe timer is TCP's tail loss probe. Sending redundant copies
to avoid a round trip is the same tradeoff as Google's Proactive TCP and
WebRTC's redundant encoding, here limited to retransmissions of losses that
look random.
