"""Read-only source connectors (Zenmoney, Teamup, Telegram) and their transport.

Every connector here is read-only by construction:

* it only ever issues HTTP ``GET`` (Teamup, Telegram history) or the documented
  read/diff call of Zenmoney,
* the Zenmoney request body is gated by an explicit allowlist of synchronisation
  markers, so a change array (transactions/accounts/tags/deletions) can never be
  sent (docs/REQUIREMENTS.md section 6.1),
* responses are normalised into domain objects, never into source payloads on the
  public side.

Credentials come from the process environment / secret store only. No connector
holds a token in a literal, and its ``repr`` never includes one.
"""

from .teamup import TeamupClient, TeamupCounts, TeamupWindow
from .telegram import ChannelSubscriberPoint, RawPost, StaticPostSource, TelegramClient, TelegramError
from .transport import (
    FakeTransport,
    HttpStatusError,
    Request,
    Response,
    Transport,
    TransportError,
    UrllibTransport,
    json_response,
)
from .zenmoney import ReadOnlyViolation, ZenmoneyClient, ZenmoneyDiff, ZenmoneyRow

__all__ = [
    "TeamupClient",
    "TeamupCounts",
    "TeamupWindow",
    "ChannelSubscriberPoint",
    "RawPost",
    "StaticPostSource",
    "TelegramClient",
    "TelegramError",
    "FakeTransport",
    "HttpStatusError",
    "Request",
    "Response",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "json_response",
    "ReadOnlyViolation",
    "ZenmoneyClient",
    "ZenmoneyDiff",
    "ZenmoneyRow",
]
