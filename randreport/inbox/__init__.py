"""The passive Telegram webhook inbox.

``docs/REQUIREMENTS.md`` section 8 allows exactly one event intake besides the
read-only history pull: a webhook that *passively and reliably* writes incoming
``channel_post``/``edited_channel_post`` updates into a private inbox, with all
processing (and every public change) happening in the 03:00 cycle.

``docs/REQUIREMENTS.md`` also says not to depend on the Bot API ``getUpdates``
queue for history: it keeps updates for at most 24 hours, so one failed day
loses data.  The inbox is the opposite of that queue — the process owns the
storage, so an update that arrived is on disk until the nightly cycle commits
it, however many days that takes.

Nothing in this package talks to Telegram: no ``setWebhook`` call, no outbound
request, no credential.  The webhook is *served*, never registered.
"""

from .import_json import ImportRejected, ImportReport, import_public_posts, parse_public_export
from .store import (
    INBOX_SOURCE,
    KINDS,
    MAX_BATCH,
    MAX_TEXT_CHARS,
    AcceptOutcome,
    InboxError,
    InboxPost,
    accept_post,
    commit_posts,
    inbox_counts,
    pending_posts,
)
from .webhook import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_UPDATES_PER_MINUTE,
    SECRET_HEADER,
    WEBHOOK_PATH,
    ForeignChannel,
    IncomingUpdate,
    TelegramWebhook,
    UpdateRejected,
    WebhookConfigError,
    parse_update,
)

__all__ = [
    "INBOX_SOURCE",
    "KINDS",
    "MAX_BATCH",
    "MAX_TEXT_CHARS",
    "AcceptOutcome",
    "InboxError",
    "InboxPost",
    "accept_post",
    "commit_posts",
    "inbox_counts",
    "pending_posts",
    "ImportRejected",
    "ImportReport",
    "import_public_posts",
    "parse_public_export",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_UPDATES_PER_MINUTE",
    "SECRET_HEADER",
    "WEBHOOK_PATH",
    "ForeignChannel",
    "IncomingUpdate",
    "TelegramWebhook",
    "UpdateRejected",
    "WebhookConfigError",
    "parse_update",
]
