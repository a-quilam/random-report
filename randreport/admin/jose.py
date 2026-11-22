"""Minimal, auditable JOSE/JWT verification for the closed editorial desk.

The admin desk trusts exactly one source of identity: the ``id_token`` of a
Telegram OIDC login (or the classic Telegram Login widget signature). Both are
public-key signatures, so this module implements the two primitives those
endpoints use and refuses everything else:

* ``ES256`` — ECDSA over NIST P-256 (pure integer arithmetic, ``pow`` for the
  modular inverse);
* ``RS256`` — RSASSA-PKCS1-v1_5 with SHA-256.

``alg: none`` and HMAC algorithms are rejected before any key is looked at:
with a JWKS as the key source, an attacker-chosen HMAC secret must never be
tried (docs/REQUIREMENTS.md section 11; acceptance A01).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


class JoseError(ValueError):
    """A token that must not be trusted."""


# --------------------------------------------------------------------------- #
# base64url
# --------------------------------------------------------------------------- #

_B64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def b64url_decode(data: str | bytes) -> bytes:
    """Strict base64url decoding: no padding, no standard-alphabet characters."""

    if isinstance(data, bytes):
        text = data.decode("ascii", "strict")
    else:
        text = data
    text = text.rstrip("=")
    if not text or any(char not in _B64_ALPHABET for char in text):
        raise JoseError("segment is not unpadded base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - defensive
        raise JoseError("segment is not decodable base64url") from exc


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_int(data: str) -> int:
    return int.from_bytes(b64url_decode(data), "big")


def _b64_fixed(data: str, size: int) -> bytes:
    raw = b64url_decode(data)
    if len(raw) != size:
        raise JoseError(f"expected a {size}-byte value")
    return raw


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RsaKey:
    kid: str
    n: int
    e: int
    alg: str = "RS256"


@dataclass(frozen=True)
class EcKey:
    kid: str
    x: int
    y: int
    crv: str = "P-256"
    alg: str = "ES256"


PublicKey = RsaKey | EcKey


def key_from_jwk(jwk: Mapping[str, Any]) -> PublicKey:
    """Build one public key from a JWK; unknown key types are refused."""

    if not isinstance(jwk, Mapping):
        raise JoseError("a JWK must be an object")
    if jwk.get("use") not in (None, "sig"):
        raise JoseError("a JWK that is not for signature use is refused")
    kid = jwk.get("kid")
    if not isinstance(kid, str) or not kid:
        raise JoseError("a JWK without a key id is refused")
    kty = jwk.get("kty")
    if kty == "RSA":
        n = _b64_int(str(jwk.get("n", "")))
        e = _b64_int(str(jwk.get("e", "")))
        if n.bit_length() < 2048:
            raise JoseError("an RSA modulus shorter than 2048 bits is refused")
        if e < 3 or e % 2 == 0:
            raise JoseError("an implausible RSA exponent is refused")
        return RsaKey(kid=kid, n=n, e=e, alg=str(jwk.get("alg") or "RS256"))
    if kty == "EC":
        if jwk.get("crv") != "P-256":
            raise JoseError("only the P-256 curve is accepted")
        x = _b64_int(str(jwk.get("x", "")))
        y = _b64_int(str(jwk.get("y", "")))
        _assert_on_curve(x, y)
        return EcKey(kid=kid, x=x, y=y, alg=str(jwk.get("alg") or "ES256"))
    raise JoseError(f"unsupported key type {kty!r}")


def keys_from_jwks(document: Mapping[str, Any]) -> dict[str, PublicKey]:
    if not isinstance(document, Mapping) or not isinstance(document.get("keys"), Sequence):
        raise JoseError("a JWKS must be an object with a keys array")
    out: dict[str, PublicKey] = {}
    for jwk in document["keys"]:
        key = key_from_jwk(jwk)
        out[key.kid] = key
    if not out:
        raise JoseError("the JWKS contains no usable key")
    return out


# --------------------------------------------------------------------------- #
# ES256 (NIST P-256)
# --------------------------------------------------------------------------- #

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _assert_on_curve(x: int, y: int) -> None:
    if not (0 <= x < _P and 0 <= y < _P):
        raise JoseError("a P-256 coordinate is out of range")
    if (y * y - (x * x * x + _A * x + _B)) % _P != 0:
        raise JoseError("a P-256 point is not on the curve")


def _point_add(left: tuple[int, int] | None, right: tuple[int, int] | None) -> tuple[int, int] | None:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    y3 = (slope * (x1 - x3) - y1) % _P
    return (x3, y3)


def _scalar_mul(scalar: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def public_point(secret: int) -> tuple[int, int]:
    """``secret * G`` — used by the tests to derive a synthetic key pair."""

    if not 1 <= secret < _N:
        raise JoseError("a P-256 private scalar must be in [1, n-1]")
    point = _scalar_mul(secret, (_GX, _GY))
    if point is None:  # pragma: no cover - impossible for a non-zero scalar below n
        raise JoseError("degenerate public point")
    return point


def sign_es256(secret: int, message: bytes, *, k: int) -> bytes:
    """A deterministic ECDSA signature, for synthetic test material only."""

    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    r = _scalar_mul(k, (_GX, _GY))
    if r is None:  # pragma: no cover - defensive
        raise JoseError("degenerate nonce")
    rx = r[0] % _N
    if rx == 0:  # pragma: no cover - defensive
        raise JoseError("degenerate nonce")
    s = pow(k, -1, _N) * (digest + rx * secret) % _N
    if s == 0:  # pragma: no cover - defensive
        raise JoseError("degenerate nonce")
    return rx.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_es256(signature: bytes, message: bytes, key: EcKey) -> bool:
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < _N and 0 < s < _N):
        return False
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, _N)
    point = _point_add(_scalar_mul(digest * w % _N, (_GX, _GY)), _scalar_mul(r * w % _N, (key.x, key.y)))
    if point is None:
        return False
    return point[0] % _N == r


# --------------------------------------------------------------------------- #
# RS256 (RSASSA-PKCS1-v1_5 / SHA-256)
# --------------------------------------------------------------------------- #

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_rs256(signature: bytes, message: bytes, key: RsaKey) -> bool:
    size = (key.n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= key.n:
        return False
    encoded = pow(value, key.e, key.n).to_bytes(size, "big")
    tail = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = size - len(tail) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + tail
    return hmac.compare_digest(encoded, expected)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #

ASYM_ALGS: Mapping[str, str] = {"ES256": "EC", "RS256": "RSA"}


@dataclass(frozen=True)
class VerifiedToken:
    header: Mapping[str, Any]
    claims: Mapping[str, Any]


def _parse_segment(segment: str, what: str) -> Mapping[str, Any]:
    try:
        value = json.loads(b64url_decode(segment))
    except (JoseError, ValueError) as exc:
        raise JoseError(f"the {what} is not JSON") from exc
    if not isinstance(value, Mapping):
        raise JoseError(f"the {what} must be a JSON object")
    return value


def _seconds(value: Any, claim: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JoseError(f"the {claim} claim must be a number")
    return float(value)


def verify_jwt(
    token: str,
    *,
    keys: Mapping[str, PublicKey],
    issuer: str | None = None,
    audience: str | Sequence[str] | None = None,
    nonce: str | None = None,
    now: datetime | None = None,
    skew_seconds: float = 60.0,
    allowed_algs: Iterable[str] = ("ES256", "RS256"),
) -> VerifiedToken:
    """Verify a signed JWT against a JWKS. Anything unexpected is refused."""

    if not isinstance(token, str) or token.count(".") != 2:
        raise JoseError("a JWT must have exactly three segments")
    header_segment, payload_segment, signature_segment = token.split(".")
    header = _parse_segment(header_segment, "header")
    claims = _parse_segment(payload_segment, "payload")

    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in set(allowed_algs) or alg not in ASYM_ALGS:
        raise JoseError(f"the token algorithm {alg!r} is not accepted")
    if "crit" in header:
        raise JoseError("critical headers are refused")
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in keys:
        raise JoseError("the token key id is not in the JWKS")
    key = keys[kid]
    if ASYM_ALGS[alg] != ("EC" if isinstance(key, EcKey) else "RSA"):
        raise JoseError("the token algorithm does not match the key type")
    if key.alg and key.alg != alg:
        raise JoseError("the key is not usable with this algorithm")

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = b64url_decode(signature_segment)
    if isinstance(key, EcKey):
        ok = verify_es256(signature, signing_input, key)
    else:
        ok = verify_rs256(signature, signing_input, key)
    if not ok:
        raise JoseError("the token signature does not verify")

    moment = (now or datetime.now(timezone.utc)).timestamp()
    if issuer is not None and claims.get("iss") != issuer:
        raise JoseError("the issuer claim does not match")
    if audience is not None:
        expected = {audience} if isinstance(audience, str) else set(audience)
        raw = claims.get("aud")
        got = {raw} if isinstance(raw, str) else set(raw) if isinstance(raw, Sequence) else set()
        if not expected & got:
            raise JoseError("the audience claim does not match")
    if "exp" not in claims:
        raise JoseError("the token has no expiry")
    if _seconds(claims["exp"], "exp") < moment - skew_seconds:
        raise JoseError("the token has expired")
    if "nbf" in claims and _seconds(claims["nbf"], "nbf") > moment + skew_seconds:
        raise JoseError("the token is not valid yet")
    if "iat" in claims and _seconds(claims["iat"], "iat") > moment + skew_seconds:
        raise JoseError("the token was issued in the future")
    if nonce is not None and claims.get("nonce") != nonce:
        raise JoseError("the nonce claim does not match")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise JoseError("the token has no stable subject")
    return VerifiedToken(header=header, claims=claims)


def jwk_thumbprint(x: int, y: int, *, kty: str = "EC", crv: str = "P-256", kid: str = "test") -> str:
    """RFC 7638 style thumbprint, used to give synthetic keys a stable id."""

    if kty == "EC":
        canonical = {"crv": crv, "kty": "EC", "x": b64url_encode(x.to_bytes(32, "big")), "y": b64url_encode(y.to_bytes(32, "big"))}
    else:  # pragma: no cover - only used for synthetic material
        canonical = {"e": kid, "kty": "RSA", "n": kid}
    digest = hashlib.sha256(json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")).digest()
    return b64url_encode(digest)
