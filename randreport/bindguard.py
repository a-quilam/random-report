"""Bind guard: the public site and the desk listen on loopback unless told otherwise.

The product serves a private projection; publishing it on a routable interface
without a TLS terminator in front would be a data-protection incident, not a
configuration preference (docs/REQUIREMENTS.md sections 9, 11). The default of the
whole stack is therefore ``127.0.0.1``, and a non-loopback bind is refused unless
it is asked for *explicitly*: both the ``--allow-external-bind`` flag and the
``RANREPORT_ALLOW_EXTERNAL_BIND=1`` environment variable must agree. A single
mistaken environment variable is not enough to expose the desk.

Loopback is decided by parsing the literal address, not by a string match, so
``0.0.0.0``, ``::``, a routable IP and a DNS name all fall through to the refusal.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Mapping

__all__ = ["LOOPBACK_HOSTS", "BindRefused", "is_loopback", "assert_bind_allowed"]

LOOPBACK_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "127.0.0.1", "::1"})

#: The opt-in a deployment must set, in addition to the flag, to bind externally.
ALLOW_ENV = "RANREPORT_ALLOW_EXTERNAL_BIND"


class BindRefused(RuntimeError):
    """A non-loopback bind was requested without the explicit double opt-in."""


def is_loopback(host: str | None) -> bool:
    """True when ``host`` is a loopback literal, ``localhost`` or empty (default)."""

    if host is None:
        return True
    candidate = str(host).strip()
    if candidate == "":
        return True
    if candidate.lower() in LOOPBACK_HOSTS:
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # a DNS name or a wildcard address: not a loopback literal
        return False


def assert_bind_allowed(
    host: str | None,
    *,
    allow_external: bool = False,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise :class:`BindRefused` unless the bind is loopback or doubly opted in.

    ``allow_external`` is the command-line flag; the environment variable is the
    deployment-level opt-in. Both are required, so neither a stray CLI argument
    nor a stray unit file ``Environment=`` line can publish the service alone.
    """

    if is_loopback(host):
        return
    env = os.environ if environ is None else environ
    env_opt_in = str(env.get(ALLOW_ENV, "")).strip() == "1"
    if allow_external and env_opt_in:
        return
    raise BindRefused(
        f"refusing to bind {host!r}: the default is loopback; "
        f"pass --allow-external-bind AND set {ALLOW_ENV}=1 to publish externally"
    )
