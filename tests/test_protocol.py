"""Unit tests for protocol.py's codec and RangeSet."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import protocol as p  # noqa: E402


def test_data_roundtrip():
    buf = p.pack_data(123456, 999, 42, 70000, b"hello", end=True, probe=True)
    assert len(buf) == p.DATA_HDR + 5
    flags, seq, ts, mid, off, pl = p.unpack_data(buf)
    assert (seq, ts, mid, off, pl) == (123456, 999, 42, 70000, b"hello")
    assert flags & p.F_END and flags & p.F_PROBE and not flags & p.F_ACK
    assert not p.is_ack(buf)


def test_ack_roundtrip():
    buf = p.pack_ack(5000, 77, 40_000, [(6000, 7000), (8000, 9000)])
    assert len(buf) == p.ACK_HDR + 2 * p.SACK_BLOCK
    assert p.is_ack(buf)
    cum, rwnd, echo, owd, sacks = p.unpack_ack(buf)
    assert (cum, rwnd, echo, owd) == (5000, p.RWND, 77, 40_000)
    assert sacks == [(6000, 7000), (8000, 9000)]


def test_ack_owd_clamped():
    _, _, _, owd, _ = p.unpack_ack(p.pack_ack(0, 0, 1 << 30))
    assert owd == (1 << 24) - 1


def test_max_payload_fits():
    assert len(p.pack_data(0, 0, 0, 0, b"x" * p.MAX_PAYLOAD)) == p.MAX_DATAGRAM


def test_rangeset_basic():
    r = p.RangeSet()
    assert r.add(10, 20)
    assert r.add(30, 40)
    assert not r.add(12, 18)
    assert r.first_end(0) == 0
    assert r.add(0, 10)
    assert r.first_end(0) == 20
    assert r.above(20) == [(30, 40)]
    assert r.add(20, 30)
    assert r.r == [[0, 40]]
    assert r.covers(0, 40) and not r.covers(0, 41)


def test_rangeset_random():
    rng = random.Random(1)
    for _ in range(200):
        r, bits = p.RangeSet(), [False] * 200
        for _ in range(30):
            lo = rng.randrange(200)
            hi = min(200, lo + rng.randrange(1, 20))
            new = r.add(lo, hi)
            assert new == (not all(bits[lo:hi]))
            for i in range(lo, hi):
                bits[i] = True
        got = [False] * 200
        for a, b in r.r:
            for i in range(a, b):
                got[i] = True
        assert got == bits
        assert all(r.r[k][1] < r.r[k + 1][0] for k in range(len(r.r) - 1))
