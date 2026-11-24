"""Ed25519 signatures (RFC 8032, section 5.1), standard library only.

Used to check licence files (v1.0.4). Verifying needs only the public key,
which is safe to publish; signing (done by the separate Licence Generator, never by
Voice Recorder Pro) needs the 32-byte secret seed.

Written from the algorithm in RFC 8032 (https://www.rfc-editor.org/rfc/rfc8032) and
checked against its test vectors in section 7.1. Points use extended homogeneous
coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z, and the complete addition
law of section 5.1.4, which also doubles. This is not constant-time; it is meant for
checking signatures and for signing on the publisher's own PC, not for a server.
This file is self-contained so the Licence Generator can use an identical copy.
"""
from __future__ import annotations

import hashlib

P = 2 ** 255 - 19                                                  # field prime
L = 2 ** 252 + 27742317777372353535851937790883648493              # order of the base point
D = -121665 * pow(121666, P - 2, P) % P                            # curve constant d
SQRT_M1 = pow(2, (P - 1) // 4, P)                                  # a square root of -1

_BY = 4 * pow(5, P - 2, P) % P                                     # base point: y = 4/5, x even


def _recover_x(y: int, sign: int) -> int | None:
    """x for a given y and sign bit (section 5.1.3), or None when there is none."""
    if y >= P:
        return None
    u = (y * y - 1) % P
    v = (D * y * y + 1) % P
    x = u * pow(v, 3, P) * pow(u * pow(v, 7, P), (P - 5) // 8, P) % P
    vx2 = v * x * x % P
    if vx2 == u:
        pass
    elif vx2 == (-u) % P:
        x = x * SQRT_M1 % P
    else:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = P - x
    return x


_BX = _recover_x(_BY, 0)
B = (_BX, _BY, 1, _BX * _BY % P)
IDENTITY = (0, 1, 1, 0)


def _add(p1: tuple, p2: tuple) -> tuple:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * D * t1 * t2 % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, pt: tuple) -> tuple:
    q = IDENTITY
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(p1: tuple, p2: tuple) -> bool:
    x1, y1, z1, _ = p1
    x2, y2, z2, _ = p2
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _encode(pt: tuple) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    n = int.from_bytes(data, "little")
    y, sign = n & ((1 << 255) - 1), n >> 255
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _h(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("an Ed25519 secret key is 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8                       # clear the 3 lowest bits and bit 255
    a |= 1 << 254                             # set bit 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a 32-byte secret seed (section 5.1.5)."""
    a, _ = _expand(seed)
    return _encode(_mul(a, B))


def sign(seed: bytes, message: bytes) -> bytes:
    """64-byte signature of `message` (section 5.1.6)."""
    a, prefix = _expand(seed)
    pub = _encode(_mul(a, B))
    r = _h(prefix + message) % L
    rb = _encode(_mul(r, B))
    k = _h(rb + pub + message) % L
    s = (r + k * a) % L
    return rb + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True when `signature` is a valid signature of `message` by `public` (section 5.1.7)."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_pt = _decode(public)
    r_pt = _decode(signature[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    k = _h(signature[:32] + public + message) % L
    left = _mul(8 * s, B)                                  # [8][S]B
    right = _add(_mul(8, r_pt), _mul(8 * k, a_pt))         # [8]R + [8][k]A'
    return _equal(left, right)
