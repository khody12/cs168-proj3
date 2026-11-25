# Design Change: Our Header Sits Inside UDP

Ellis O'Dowd, Eric Khodorenko

**Summary:** our design document describes a transport protocol that sits
directly on IP, the way TCP does. The project, though, only lets us send UDP
packets, so our header ends up inside UDP's. UDP already provides ports and a
checksum, so we drop those fields from our header. We also drop the handshake
flags and hop records, and add a few fields the new design needs. Everything
else (sequence numbers, cumulative ACKs, SACK, the receive window) stays as
the design doc described it.

The implementation details are in [README.md](README.md).

---

## What's going on

Networking works in layers. IP gets a packet from one machine to another.
On top of IP sits a *transport* protocol such as TCP, which turns that into
reliable, well-paced delivery. Our project is to design that transport
layer, and the design doc drew it directly on IP:

```
What the design doc describes:   [ IP ][ our header ][ data ]
```

But the course harness only gives us one way to send: `send_datagram()`, which
always sends a UDP packet. We can't build raw IP packets, and the harness
charges for the IP and UDP headers on every packet whether we want them or
not. So in practice we have:

```
What actually goes on the wire:  [ IP ][ UDP ][ our header ][ data ]
```

UDP is a very thin layer. It adds source and destination ports, a length, and
a checksum, and nothing else: no reliability, no ordering, no congestion
control. All of that is still our job. Running a new protocol inside UDP like
this is normal; QUIC, the protocol behind HTTP/3, does exactly the same thing.

## What we drop, and why

**Source and destination ports.** UDP's header already has both, and the
harness uses them to deliver each packet to the right endpoint. The receiver
learns who sent a packet from UDP's address. Our own copies would just repeat
UDP's.

**Checksum.** UDP's checksum already covers everything inside the UDP packet,
which is our whole header and data. A second checksum using the same algorithm
over the same bytes doesn't catch anything the first one misses. (If we ever
wanted stronger protection for a real deployment, the right move would be a
better checksum such as CRC32C, not a duplicate of the same one.)

**Handshake flags (SYN, FIN, RST).** The design doc opened each connection
with a SYN exchange. In this network the best possible time for a round of
traffic is about half a round trip, so a handshake alone adds more delay than
the entire ideal round. Instead, the first packet carries data. The receiver
creates its state for a sender the first time it hears from it, and there's
nothing to tear down at the end. The full reasoning is in
[CHANGED_HANDSHAKE_DESIGN.md](CHANGED_HANDSHAKE_DESIGN.md).

**Hop count and hop records.** Switches in this project can't write into our
packets, so these would always be empty. See
[CHANGED_ROUTER_STAMPING_DESIGN.md](CHANGED_ROUTER_STAMPING_DESIGN.md).

## What we keep

- **Sequence numbers**, counting bytes like the design doc: each sender→receiver
  pair has its own byte stream, and each packet says where its bytes start.
- **Cumulative ACK and SACK blocks**, as designed. The receiver says "I have
  everything up to here" plus a few ranges it has received beyond that, so
  the sender knows exactly what to resend. We'll cap how many SACK blocks an
  ACK carries (around 4) to keep ACKs small; the count field stays.
- **Receive window.** The simulator never fills a receiver's memory, but a
  real receiver has limited buffer space, so we keep flow control in the
  design.
- **The flags byte**, now holding ACK, END and PROBE flags instead of the
  handshake flags.

## What we add

- **A send timestamp** on every data packet. This is how we measure
  congestion now that switches can't report it.
- **Message ID and offset within the message.** Each packet says which
  message its bytes belong to and where they go. That lets the receiver
  deliver a message as soon as all of its pieces have arrived, even if an
  earlier packet was lost, and lets us split large messages across packets.
  An END flag marks the packet holding a message's last byte.
- **In the ACK: the echoed timestamp and the measured one-way delay**, so
  the sender learns both the round-trip time and how long its packet waited in
  queues.

## Two headers instead of one

The design doc used one header for everything, like TCP, where a packet can
carry data and an acknowledgment at the same time. In this project, senders
and receivers are separate endpoints, so data only ever flows one way and
ACKs only ever flow back. A data packet never needs an ACK number, and an ACK
never carries data. So we use two small headers that share the same first byte
(the flags):

```
Data packet (16 bytes)
  flags | #SACK      1   ACK flag off; END marks a message's last packet
  sequence number    4   byte offset in this sender→receiver stream
  send timestamp     4   microseconds, shared clock
  message ID         4
  offset in message  3

ACK packet (14 bytes + 8 per SACK block)
  flags | #SACK      1   ACK flag on; #SACK = number of blocks that follow
  cumulative ACK     4   next byte expected; everything before it has arrived
  receive window     2   free buffer space, as in the design doc
  echoed timestamp   4   copied from the data packet being acknowledged
  one-way delay      3   microseconds the packet spent getting here
  SACK blocks        8   each: start and end of a received range (cap ~4)
```

For comparison, the design doc's header was 18 bytes before hop records, and a
packet crossing the spine would have collected 33 more bytes of hop records.
The new data header is 16 bytes, and the usual ACK is 14.
