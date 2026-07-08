"""
d2a/_ed25519_fallback.py — pure-Python RFC 8032 Ed25519.

╔══════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEMO-GRADE ONLY — NOT CONSTANT TIME — DO NOT USE IN PRODUCTION.      ║
║                                                                          ║
║  This is a straight-from-the-spec reference implementation of Ed25519    ║
║  (RFC 8032). It exists so the D2A core has ZERO third-party crypto       ║
║  dependencies and still produces real, verifiable signatures on a bare   ║
║  Python install.                                                         ║
║                                                                          ║
║  It uses Python big-integer arithmetic with data-dependent branches and  ║
║  a variable-time scalar multiply. It IS therefore vulnerable to timing   ║
║  side channels that can leak the signing key. It is also slow.           ║
║                                                                          ║
║  Production deployments MUST install the [crypto] extra (PyNaCl or        ║
║  `cryptography`); d2a.crypto auto-detects and prefers those backends.    ║
║  The wire format and signatures are byte-identical either way, so this   ║
║  fallback is safe to interoperate with — it is only unsafe to RUN with a ║
║  key you care about.                                                     ║
╚══════════════════════════════════════════════════════════════════════════╝

Reference: RFC 8032, Section 6 (the Python reference code), lightly adapted.
Public surface (all operate on raw bytes):
    secret_to_public(seed32) -> pub32
    sign(seed32, msg)        -> sig64
    verify(pub32, msg, sig64) -> bool
"""

import hashlib

# ── field / group constants ─────────────────────────────────────────────────
p = 2 ** 255 - 19                                   # field prime
_d = -121665 * pow(121666, p - 2, p) % p            # curve constant d
q = 2 ** 252 + 27742317777372353535851937790883648493   # group order (L)

_modp_sqrt_m1 = pow(2, (p - 1) // 4, p)             # sqrt(-1) mod p


def _sha512(s: bytes) -> bytes:
    return hashlib.sha512(s).digest()


def _sha512_modq(s: bytes) -> int:
    return int.from_bytes(_sha512(s), "little") % q


# ── points in extended homogeneous coordinates (X, Y, Z, T) ─────────────────

def _point_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * _d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _point_mul(s: int, P):
    Q = (0, 1, 1, 0)                                # neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P, Q) -> bool:
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:
        return False
    return True


def _recover_x(y: int, sign: int):
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, p - 2, p) % p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * _modp_sqrt_m1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


# base point B = (recover_x(4/5), 4/5)
_g_y = 4 * pow(5, p - 2, p) % p
_g_x = _recover_x(_g_y, 0)
_G = (_g_x, _g_y, 1, _g_x * _g_y % p)


def _point_compress(P) -> bytes:
    zinv = pow(P[2], p - 2, p)
    x = P[0] * zinv % p
    y = P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def _secret_expand(secret: bytes):
    if len(secret) != 32:
        raise ValueError("Ed25519 seed must be exactly 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return a, h[32:]


# ── public surface ──────────────────────────────────────────────────────────

def secret_to_public(secret: bytes) -> bytes:
    a, _ = _secret_expand(secret)
    return _point_compress(_point_mul(a, _G))


def sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _secret_expand(secret)
    A = _point_compress(_point_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    R = _point_mul(r, _G)
    Rs = _point_compress(R)
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _point_decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _point_decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = _sha512_modq(Rs + public + msg)
    sB = _point_mul(s, _G)
    hA = _point_mul(h, A)
    return _point_equal(sB, _point_add(R, hA))
