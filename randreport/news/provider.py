"""The configurable LLM adapter for the daily news pass.

``docs/REQUIREMENTS.md`` section 8 asks for a cheap, *configured* provider with a
cost/token/retry limit, a cache and a deferred queue — and the card adds three
hard requirements this module is built around:

* **off by default.**  :func:`config_from_env` starts at ``provider=none`` with a
  **budget of 0**, so a fresh deployment makes no paid request at all; enabling
  the adapter takes a provider *and* an explicit positive budget.  With no
  provider the news section reports an honest gap instead of pretending;
* **the model gets nothing but the posts.**  The adapter is a pure text function:
  it holds no database handle, no tool list, no file access and no secret other
  than its own API key, which travels in the ``Authorization`` header and never
  in a URL or a log line;
* **retries are bounded and rate limits are respected.**  An HTTP 429 becomes
  :class:`LlmRateLimited`, which the extractor turns into a *deferred* job rather
  than a retry loop; a timeout is a bounded retry, then a degradation.

The concrete wire format is the widely-supported OpenAI-compatible
``/chat/completions`` shape; nothing here is hardcoded to one vendor beyond that
one documented path, which the operator can override.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from ..connectors.transport import HttpStatusError, Request, Response, Transport, TransportError

PROVIDER_NONE = "none"
DEFAULT_API_PATH = "/v1/chat/completions"
DEFAULT_TIMEOUT_S = 30.0
#: the adapter is off until an operator enables it: no provider, no budget
DEFAULT_MAX_CALLS_PER_DAY = 0
#: 0 leaves the token dimension uncapped; the call budget is the gate
DEFAULT_MAX_TOKENS_PER_DAY = 0
MIN_API_KEY_CHARS = 8
MAX_OUTPUT_CHARS = 200_000


class LlmConfigError(ValueError):
    """A provider was selected but the configuration is incomplete."""


class LlmProviderError(RuntimeError):
    """A provider call that did not produce a usable answer."""


class LlmRateLimited(LlmProviderError):
    """HTTP 429: the provider asked to be left alone; defer, never retry-storm."""


class LlmUnauthorized(LlmProviderError):
    """HTTP 401/403: the key is wrong or revoked."""


class LlmTransportError(LlmProviderError):
    """A network-level failure (timeout, connection reset)."""


class LlmProtocolError(LlmProviderError):
    """A 2xx response that does not carry the documented answer shape."""


@dataclass(frozen=True)
class LlmConfig:
    """Everything about the adapter, all of it configurable and all of it inert by default."""

    provider: str = PROVIDER_NONE
    base_url: str = ""
    model: str = ""
    api_path: str = DEFAULT_API_PATH
    api_key_set: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    retries: int = 2
    max_calls_per_day: int = DEFAULT_MAX_CALLS_PER_DAY
    max_tokens_per_day: int = DEFAULT_MAX_TOKENS_PER_DAY

    @property
    def enabled(self) -> bool:
        """A provider name other than ``none``/empty is the only way in."""

        return bool(self.provider) and self.provider.strip().lower() != PROVIDER_NONE

    @property
    def budget(self) -> bool:
        """True when the operator explicitly allowed paid calls for the day."""

        return self.max_calls_per_day > 0


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise LlmConfigError(f"{name} must be an integer") from exc
    if value < 0:
        raise LlmConfigError(f"{name} must not be negative")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise LlmConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise LlmConfigError(f"{name} must be positive")
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> LlmConfig:
    """Read the adapter configuration; the default is "off with a budget of zero"."""

    source = os.environ if env is None else env
    provider = (source.get("NEWS_LLM_PROVIDER") or PROVIDER_NONE).strip()
    return LlmConfig(
        provider=provider,
        base_url=(source.get("NEWS_LLM_BASE_URL") or "").strip(),
        model=(source.get("NEWS_LLM_MODEL") or "").strip(),
        api_path=(source.get("NEWS_LLM_API_PATH") or DEFAULT_API_PATH).strip() or DEFAULT_API_PATH,
        api_key_set=bool((source.get("NEWS_LLM_API_KEY") or "").strip()),
        timeout_s=_float(source, "NEWS_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S),
        retries=_int(source, "NEWS_LLM_RETRIES", 2),
        max_calls_per_day=_int(source, "NEWS_LLM_MAX_CALLS_PER_DAY", DEFAULT_MAX_CALLS_PER_DAY),
        max_tokens_per_day=_int(source, "NEWS_LLM_MAX_TOKENS_PER_DAY", DEFAULT_MAX_TOKENS_PER_DAY),
    )


class ChatCompletionsProvider:
    """The real, configurable HTTP adapter (OpenAI-compatible chat completions)."""

    def __init__(
        self,
        transport: Transport,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_path: str = DEFAULT_API_PATH,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        if not base_url or not model:
            raise LlmConfigError("base_url and model are required for the HTTP provider")
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._api_path = api_path if api_path.startswith("/") else "/" + api_path
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_output_chars = max_output_chars
        self.model = model

    def __repr__(self) -> str:
        return (
            f"ChatCompletionsProvider(base_url={self._base_url!r}, model={self.model!r}, "
            f"api_key={'set' if self._api_key else None})"
        )

    def complete(self, *, system: str, prompt: str, timeout_s: float | None = None) -> str:
        """One completion. Raises a typed error; it never returns a broken answer."""

        request = Request(
            method="POST",
            url=f"{self._base_url}{self._api_path}",
            json_body={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            headers=self._headers(),
            timeout_s=timeout_s or self._timeout_s,
        )
        try:
            response = self._transport.send(request)
        except HttpStatusError as exc:
            raise self._status_error(exc) from exc
        except TransportError as exc:
            raise LlmTransportError(str(exc)) from exc
        if not response.ok:
            raise self._status_error(HttpStatusError(response.status, response.text()))
        return self._extract(response)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            # the key travels in a header, never in the URL or a log line
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _status_error(error: HttpStatusError) -> LlmProviderError:
        if error.status == 429:
            return LlmRateLimited("the provider rate-limited the request (HTTP 429)")
        if error.status in (401, 403):
            return LlmUnauthorized(f"the provider refused the credential (HTTP {error.status})")
        return LlmProviderError(f"the provider answered HTTP {error.status}")

    def _extract(self, response: Response) -> str:
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise LlmProtocolError("the provider returned a non-JSON body") from exc
        if not isinstance(payload, Mapping):
            raise LlmProtocolError("the provider returned a non-object body")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmProtocolError("the provider returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            # some providers answer with content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, Mapping))
        if not isinstance(content, str) or not content.strip():
            raise LlmProtocolError("the provider returned an empty completion")
        if len(content) > self._max_output_chars:
            raise LlmProtocolError(f"the completion is longer than {self._max_output_chars} characters")
        return content


def provider_from_config(
    transport: Transport, config: LlmConfig, *, api_key: str | None = None
) -> ChatCompletionsProvider | None:
    """Build the adapter, or return ``None`` for the honest "no provider" gap."""

    if not config.enabled:
        return None
    if not config.base_url:
        raise LlmConfigError("NEWS_LLM_BASE_URL is required when a provider is selected")
    if not config.model:
        raise LlmConfigError("NEWS_LLM_MODEL is required when a provider is selected")
    if api_key is not None and len(api_key.strip()) < MIN_API_KEY_CHARS:
        raise LlmConfigError("NEWS_LLM_API_KEY is too short to be a credential")
    return ChatCompletionsProvider(
        transport,
        base_url=config.base_url,
        model=config.model,
        api_key=api_key,
        api_path=config.api_path,
        timeout_s=config.timeout_s,
    )
