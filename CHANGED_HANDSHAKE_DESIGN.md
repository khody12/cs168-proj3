# Design Change: No Handshake

Ellis O'Dowd, Eric Khodorenko

**Summary:** our design document opened each connection with a SYN / SYN-ACK
exchange and closed it with FIN. We're dropping both. The first packet a
sender sends already carries data, and the receiver sets up its state when
that packet arrives. In this network, the handshake's round trip alone takes
longer than an entire ideal round of traffic, and none of the jobs a handshake
normally does are needed here.

The implementation details are in [README.md](README.md).

---

## What we originally planned

The timing diagram in our design doc follows TCP: the sender sends a SYN, the
receiver answers with a SYN-ACK, and the first data goes out alongside the
sender's final ACK. At the end, a FIN / FIN-ACK exchange closes the
connection.

## The problem: it costs a whole round trip

A handshake means waiting one full round trip before the first byte of data
can leave the sender.

In this scenario, the grading compares each round against the fastest the
network could possibly finish it. That ideal is roughly the time for data to
travel *one way*, plus the time to push the bytes through the busiest link.
For a small round, that's only about half a round trip.

So a handshake on its own adds more delay than the whole ideal round, and we'd
fail the speed requirement before a single packet was lost. Shaving a round
trip isn't a minor optimization here; it decides whether we pass.

## Background: messages and message IDs

A lot of what follows relies on how this project hands us data, so it helps to
spell that out first.

TCP has no idea what a message is. The application gives it a stream of
bytes, and TCP delivers the same bytes in order, with no boundaries. If an app
writes "hello" and then "world", the other side might read "hellowor" and then
"ld". Applications that care about boundaries add their own on top.

This project works differently. The harness plays the application on both
ends, and it deals in whole **messages**:

- On the sending server, it hands our sender one complete message at a time
  (`on_app_data`), each with a **message ID**, a size, its bytes, and a
  destination.
- On the receiving server, our receiver has to give each message back to the
  application whole, with the same ID (`self.deliver(msg_id, bytes)`). That
  call is also where the harness checks the bytes and records the arrival
  time for grading.

The harness numbers every message in the trial 0, 1, 2, … from one central
list before the trial starts, so **every message ID is unique across the whole
trial**, not just between one sender and one receiver.

Our protocol carries that ID with every piece of a message it sends, along
with the offset of the piece within the message, and an END flag on the
message's last piece (one packet can carry pieces of several messages).
The receiver keeps one "bucket" per message ID. Each piece says which
bucket it belongs in and where its bytes go. When a bucket is full, the
message is delivered. Separately, the stream sequence numbers are only our own
bookkeeping for ACKs, so the sender knows what to resend. The application
never sees them.

In a real deployment nobody would number messages centrally like this. Our
protocol would count messages itself, per sender→receiver pair, and the
receiver would identify a message by (sender, message number) instead of the
ID alone. Numbers would only be unique within each sender, so two senders
could both send a "message 5" to the same receiver; pairing the number with
the sender's address (which UDP already gives us) keeps them apart.

**Why messages help.** Using messages says that each message must arrive
complete, but different messages don't depend on each other. TCP's byte
stream promises in-order delivery instead, so a hole blocks everything after
it. If bytes 100–200 are missing, TCP can hand the application bytes 0–99,
but nothing after byte 200, even if all of that data has already arrived,
until the hole is filled. With messages, if we have messages 2–8 but not
message 1, we can still deliver 2–8 right away because each one is
self-contained. Only message 1 waits for its retransmission. This problem is
called head-of-line blocking, and avoiding it is a big reason newer
protocols (QUIC, Homa) moved to messages or independent streams.

## Why it's safe to skip

A handshake normally does four jobs. Here, each one is either unnecessary or
handled some other way.

**Agreeing on starting sequence numbers.** TCP picks a random starting number
for each connection, so a delayed packet from an old connection can't be
mistaken for new data. Here, each sender→receiver pair has exactly one stream
for the whole trial, and it starts at byte 0. Connections never close and
reopen, so there are no old connections for stray packets to come from. And
because every message ID is unique across the trial (see above), even a very
late or duplicated packet goes into the right message's bucket, or is
recognized as belonging to a message that's already been delivered and is
ignored.

**Checking that the other side is there.** On the open internet you don't
know whether a server is listening, so you ask first. Here every peer is known
from the start and every receiver runs for the whole trial. If a receiver is
unreachable (a failed server, say), the handshake wouldn't help: the SYN would
be lost just like data would. Retransmission handles that case either way.

**Negotiating options** such as the maximum packet size and whether SACK is
supported. TCP needs this because the two ends may run different software.
Both of our ends run the same code, so these are just constants.

**Security**, such as defending against floods of fake connection requests.
This is a closed datacenter network with no attackers, and in a real
datacenter it would be a trusted internal network.

## Why we don't need FIN either

FIN tells the other side "my byte stream is finished" so both ends can clean
up. We send messages, not one open-ended stream. Each message's last piece
carries an END flag, and the receiver delivers the message as soon as all of
its pieces are in. There's never a stream to close, and the state a receiver
keeps per sender is small enough to simply keep until the trial ends.

## What replaces it

- **Data in the first packet.** A sender starts sending the moment it has a
  message, at a rate set from the topology.
- **The receiver creates its state on first contact.** The first packet from
  a new sender sets up that sender's stream at the receiver.
- **Sequence numbers start at 0** for every sender→receiver pair.
- **Ordinary reliability covers the first packets too.** Every packet is
  acknowledged, and anything lost (including a flow's very first packet) is
  resent by the same SACK and retransmit-timer machinery as everything else.
- **The flags byte is reused.** SYN, FIN and RST are gone; the flags now mark
  ACKs and probe retransmissions. Each message's END marker lives in its own
  message entry, since one packet can carry several messages.

## Real-world precedent

Skipping the handshake round trip is a known, deliberate move. TCP Fast Open
lets a client put data in its very first packet, and QUIC (the protocol under
HTTP/3) has a "0-RTT" mode that does the same. Both exist because a round trip
spent setting up is often the largest delay a short transfer sees, which is
exactly our situation.

## A middle ground we considered

Each sender could handshake once at the start of the trial and keep its
connections open for every round after. Only the first round would pay the
extra round trip. We decided against it: it adds code and a special first
round, and it buys nothing the points above don't already cover.
