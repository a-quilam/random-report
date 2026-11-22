"""The closed editorial desk: Telegram login, verification, moderation, publication.

Modules:

``jose``            dependency-free ES256/RS256 verification
``oidc``            Telegram OIDC (PKCE) and the classic login widget
``members``         the allowlist of active commune members (deny by default)
``sessions``        sessions, CSRF tokens, login throttling
``checks``          verification stored per entity *and* revision
``diagnostics``     reconciliation findings and issues
``history_admin``   historical observations, conflicts, gaps
``news``            chronicle moderation (edit / merge / hide / restore)
``desk``            overview, money, events, channel, settings
``reports``         monthly draft post, closing and correcting versions
``render``          SSR of the desk (no JavaScript, everything escaped)
``app``             the WSGI application and its auth gate
"""

from .app import AdminDesk, build_desk_from_env

__all__ = ["AdminDesk", "build_desk_from_env"]
