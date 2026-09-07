"""Provider-agnostic LLM client layer.

The Browser Agent does NOT hardcode any specific LLM provider. This module
defines a clean `LLMClient` abstract interface and a set of concrete
implementations:

- `ZaiLLMClient`            — calls the `z-ai` CLI as a subprocess
- `OpenAICompatLLMClient`   — speaks the OpenAI Chat Completions REST API
                              (works with OpenAI, Azure OpenAI, OpenRouter,
                              Together, Anyscale, vLLM, Ollama, LM Studio,
                              and any OpenAI-compatible endpoint)
- `MockLLMClient`           — canned responses for tests

A `make_llm_client(config)` factory selects the right implementation based
on `LLMConfig.provider`. The Browser Agent receives `(provider, model,
api_key, base_url)` via settings and calls the factory once per task.

All implementations share:
- async `complete()`
- bounded timeout
- cooperative cancellation
- structured JSON output (via `LLMResponse.parsed_json`)
- error classification (`LLMUnavailableError`, `LLMTimeoutError`,
  `LLMAuthError`, `LLMRateLimitError`, `LLMResponseError`)
"""
from __future__ import annotations

import abc
import asyncio
import base64
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Literal

from ..cancel import CancellationManager, TaskCancelledError
from ..logging import get_logger

log = get_logger("sera.task.llm_client")


# ===========================================================================
# Error classification
# ===========================================================================


class LLMError(Exception):
    """Base class for all LLM client errors."""


class LLMUnavailableError(LLMError):
    """The provider cannot be reached / is not installed / not configured."""


class LLMTimeoutError(LLMError):
    """The LLM call exceeded its timeout."""


class LLMAuthError(LLMError):
    """Authentication failed (bad API key, expired token, etc.)."""


class LLMRateLimitError(LLMError):
    """The provider returned a rate-limit / quota-exceeded response."""


class LLMResponseError(LLMError):
    """The provider returned a malformed or empty response."""


# ===========================================================================
# Config + response
# ===========================================================================


@dataclass
class LLMConfig:
    """Provider-agnostic LLM configuration.

    The Browser Agent receives these values (via `Settings` env vars or via
    the `POST /tasks` request) and passes them to `make_llm_client()`.
    """

    provider: Literal["none", "zai", "openai"] = "none"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_s: float = 30.0
    # Provider-specific extras (e.g. CLI path for `zai`, API version for Azure).
    extra: dict[str, Any] = field(default_factory=dict)

    def masked_repr(self) -> str:
        """A log-safe repr with secrets masked."""
        key = self.api_key
        masked_key = (key[:4] + "…" + key[-4:]) if len(key) > 8 else "***" if key else ""
        return (
            f"LLMConfig(provider={self.provider!r}, model={self.model!r}, "
            f"api_key={masked_key!r}, base_url={self.base_url!r}, "
            f"timeout_s={self.timeout_s})"
        )


@dataclass
class LLMResponse:
    """A normalized LLM completion response.

    `content` is the raw text the model returned. `parsed_json` is a helper
    that attempts to parse `content` as JSON (tolerant of markdown fences)
    and caches the result. Callers that need structured output should use
    `parsed_json` rather than re-parsing `content` themselves.
    """

    content: str
    raw: Any = None
    provider: str = ""
    model: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    _parsed: Any = None
    _parsed_attempted: bool = False

    def __repr__(self) -> str:
        snip = (self.content or "")[:80].replace("\n", " ")
        return f"LLMResponse(provider={self.provider!r}, content={snip!r}...)"

    @property
    def parsed_json(self) -> Any:
        """Parse `content` as JSON. Tolerates markdown fences + leading prose.

        Returns the parsed object, or None if the content is not valid JSON.
        The result is cached so repeated access is free.
        """
        if self._parsed_attempted:
            return self._parsed
        self._parsed_attempted = True
        if not self.content:
            return None
        text = self.content.strip()
        # Strip markdown code fences if present.
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1 :]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        # Try direct parse first.
        try:
            self._parsed = json.loads(text)
            return self._parsed
        except json.JSONDecodeError:
            pass
        # Fallback: find the first '{' and the matching last '}'.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                self._parsed = json.loads(text[start : end + 1])
                return self._parsed
            except json.JSONDecodeError:
                pass
        self._parsed = None
        return None


# ===========================================================================
# Abstract base
# ===========================================================================


class LLMClient(abc.ABC):
    """Provider-agnostic LLM client interface.

    Implementations MUST:
    - respect the cancellation token (raise `TaskCancelledError` if cancelled)
    - bound the call by `timeout_s`
    - raise the appropriate `LLM*Error` subclass on failure
    - return an `LLMResponse` with `content` (str) on success

    Implementations MAY:
    - populate `LLMResponse.usage`, `finish_reason`, `provider`, `model`
    - cache the underlying client / connection
    - call `available()` lazily
    """

    @abc.abstractmethod
    def available(self) -> bool:
        """True if the client can be used right now (deps installed, configured)."""

    @abc.abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout_s: float,
        cancellation: CancellationManager,
        **kwargs: Any,
    ) -> LLMResponse:
        ...

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    def supports_vision(self) -> bool:
        """Whether this client can accept image inputs. Override in subclasses."""
        return False

    async def complete_with_vision(
        self,
        *,
        system: str,
        user: str,
        image_png: bytes,
        timeout_s: float,
        cancellation: CancellationManager,
    ) -> LLMResponse:
        """Default: vision not supported. Override in vision-capable clients."""
        raise LLMUnavailableError(
            f"provider {self.provider_name!r} does not support vision"
        )


# ===========================================================================
# Mock client (for tests)
# ===========================================================================


@dataclass
class MockLLMClient(LLMClient):
    """Returns canned responses, in order.

    Each entry in `responses` is either:
    - a string (returned as-is as content), or
    - a callable `(system, user) -> str` for dynamic responses, or
    - an Exception instance to be raised (e.g. LLMTimeoutError).

    If `responses` is exhausted, raises LLMUnavailableError.
    """

    responses: list[Any] = field(default_factory=list)
    _idx: int = 0
    _calls: list[dict[str, Any]] = field(default_factory=list)
    _provider_name: str = "mock"
    _supports_vision: bool = False

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    def available(self) -> bool:
        return True

    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout_s: float,
        cancellation: CancellationManager,
        **kwargs: Any,
    ) -> LLMResponse:
        cancellation.check()
        self._calls.append({"system": system, "user": user})
        if self._idx >= len(self.responses):
            raise LLMUnavailableError("MockLLMClient exhausted")
        resp = self.responses[self._idx]
        self._idx += 1
        if isinstance(resp, BaseException):
            raise resp
        if callable(resp):
            resp = resp(system, user)
        await asyncio.sleep(0)
        cancellation.check()
        return LLMResponse(content=str(resp), provider=self._provider_name)

    async def complete_with_vision(
        self,
        *,
        system: str,
        user: str,
        image_png: bytes,
        timeout_s: float,
        cancellation: CancellationManager,
    ) -> LLMResponse:
        if not self._supports_vision:
            return await self.complete(
                system=system,
                user=user + "\n\n[vision requested but mock has no vision capability]",
                timeout_s=timeout_s,
                cancellation=cancellation,
            )
        # If vision is supported, treat like a normal call but record the image.
        self._calls.append({"system": system, "user": user, "image_bytes": len(image_png)})
        return await self.complete(
            system=system, user=user, timeout_s=timeout_s, cancellation=cancellation
        )

    @property
    def calls(self) -> list[dict[str, Any]]:
        return list(self._calls)


# ===========================================================================
# z-ai CLI client
# ===========================================================================


@dataclass
class ZaiLLMClient(LLMClient):
    """Calls the `z-ai` CLI as a subprocess.

    The CLI is `z-ai-web-dev-sdk`. We use a subprocess to keep the Python
    package free of a hard Node dependency at import time.
    """

    cli_path: str = "z-ai"
    model: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def provider_name(self) -> str:
        return "zai"

    def available(self) -> bool:
        return shutil.which(self.cli_path) is not None

    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout_s: float,
        cancellation: CancellationManager,
        **kwargs: Any,
    ) -> LLMResponse:
        cancellation.check()
        if not self.available():
            raise LLMUnavailableError(f"z-ai CLI not found at {self.cli_path!r}")

        # Pass the prompt directly as a command-line argument.
        # `asyncio.create_subprocess_exec` does NOT go through a shell, so
        # there's no shell-escaping risk. Linux ARG_MAX is typically >=128KB,
        # and our state view is capped at ~12KB.
        cmd = [self.cli_path, "chat", "--prompt", user]
        if system:
            cmd += ["--system", system]

        env = dict(os.environ)
        env.update(self.extra_env)

        log.debug(
            "calling z-ai CLI",
            extra={"event": "llm.call", "provider": "zai", "prompt_chars": len(user), "timeout_s": timeout_s},
        )
        try:
            result = await cancellation.wait_or_cancel(
                _run_subprocess(cmd, env=env),
                timeout=timeout_s,
            )
        except TaskCancelledError:
            raise
        except asyncio.TimeoutError:
            raise LLMTimeoutError(f"z-ai CLI exceeded {timeout_s}s timeout") from None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            raise LLMUnavailableError(f"z-ai CLI exited {result.returncode}: {stderr}")

        stdout = result.stdout.decode("utf-8", errors="replace")
        content = _extract_openai_content(stdout)
        if not content:
            raise LLMResponseError("z-ai CLI returned empty content")

        return LLMResponse(
            content=content,
            raw=stdout,
            provider="zai",
            model=self.model or "zai-default",
        )


# ===========================================================================
# OpenAI-compatible HTTP client (works with OpenAI, Azure, OpenRouter, etc.)
# ===========================================================================


@dataclass
class OpenAICompatLLMClient(LLMClient):
    """OpenAI Chat Completions REST API client.

    Works with any OpenAI-compatible endpoint:
    - OpenAI (https://api.openai.com/v1)
    - Azure OpenAI (set base_url + api_version header)
    - OpenRouter (https://openrouter.ai/api/v1)
    - Together / Anyscale / vLLM / Ollama / LM Studio

    Uses `httpx.AsyncClient` (already a project dependency). Does NOT require
    the `openai` Python package — we speak the REST API directly so we have
    full control over timeouts, cancellation, and error classification.
    """

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    # Optional extra headers (e.g. OpenRouter's HTTP-Referer / X-Title).
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Whether the configured model accepts image inputs.
    vision_capable: bool = False
    _client: Any = None

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def supports_vision(self) -> bool:
        return self.vision_capable

    def available(self) -> bool:
        # We require an API key and a model. The base_url has a sensible
        # default, but if the user set one we trust it.
        return bool(self.api_key and self.model)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            headers.update(self.extra_headers)
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    async def _post_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        timeout_s: float,
        cancellation: CancellationManager,
    ) -> dict[str, Any]:
        """POST /chat/completions and return the parsed JSON body.

        Classifies HTTP errors into the LLM*Error hierarchy.
        """
        cancellation.check()
        client = self._ensure_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        url = "/chat/completions"
        try:
            log.debug(
                "calling OpenAI-compatible API",
                extra={
                    "event": "llm.call",
                    "provider": "openai",
                    "model": self.model,
                    "base_url": self.base_url,
                    "msg_count": len(messages),
                    "timeout_s": timeout_s,
                },
            )
            response = await cancellation.wait_or_cancel(
                client.post(url, json=payload),
                timeout=timeout_s,
            )
        except TaskCancelledError:
            raise
        except asyncio.TimeoutError:
            raise LLMTimeoutError(
                f"OpenAI-compatible API exceeded {timeout_s}s timeout"
            ) from None
        except Exception as e:
            # Network errors, DNS failures, connection refused.
            raise LLMUnavailableError(f"HTTP request failed: {e}") from e

        if response.status_code >= 400:
            body = response.text[:500]
            if response.status_code in (401, 403):
                raise LLMAuthError(
                    f"auth failed ({response.status_code}): {body}"
                )
            if response.status_code == 429:
                raise LLMRateLimitError(f"rate limited: {body}")
            if response.status_code >= 500:
                raise LLMUnavailableError(
                    f"server error {response.status_code}: {body}"
                )
            raise LLMResponseError(
                f"HTTP {response.status_code}: {body}"
            )

        try:
            data = response.json()
        except Exception as e:
            raise LLMResponseError(f"non-JSON response: {response.text[:200]}") from e

        return data

    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout_s: float,
        cancellation: CancellationManager,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.available():
            raise LLMUnavailableError(
                "OpenAI-compatible client not configured (api_key + model required)"
            )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        data = await self._post_chat(
            messages=messages, timeout_s=timeout_s, cancellation=cancellation
        )
        return self._extract_response(data)

    async def complete_with_vision(
        self,
        *,
        system: str,
        user: str,
        image_png: bytes,
        timeout_s: float,
        cancellation: CancellationManager,
    ) -> LLMResponse:
        if not self.vision_capable:
            raise LLMUnavailableError(
                f"model {self.model!r} is not vision_capable; set vision_capable=True"
            )
        if not self.available():
            raise LLMUnavailableError(
                "OpenAI-compatible client not configured (api_key + model required)"
            )
        b64 = base64.b64encode(image_png).decode("ascii")
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                        },
                    },
                ],
            }
        )
        data = await self._post_chat(
            messages=messages, timeout_s=timeout_s, cancellation=cancellation
        )
        return self._extract_response(data)

    @staticmethod
    def _extract_response(data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(f"no choices in response: {str(data)[:200]}")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("malformed choice entry")
        msg = first.get("message", {})
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or not content:
            raise LLMResponseError("empty content in response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        finish = first.get("finish_reason")
        model = data.get("model", "")
        return LLMResponse(
            content=content,
            raw=data,
            provider="openai",
            model=model,
            usage=usage,
            finish_reason=finish,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


# ===========================================================================
# Factory
# ===========================================================================


def make_llm_client(config: LLMConfig) -> LLMClient:
    """Construct the appropriate LLMClient for `config.provider`.

    Returns a `MockLLMClient`-like sentinel for `provider="none"` that is
    never available, so callers can uniformly call `client.available()`.
    """
    if config.provider == "none":
        return _NoneLLMClient()
    if config.provider == "zai":
        return ZaiLLMClient(
            cli_path=config.extra.get("cli_path", "z-ai"),
            model=config.model or "zai-default",
        )
    if config.provider == "openai":
        return OpenAICompatLLMClient(
            api_key=config.api_key,
            base_url=config.base_url or "https://api.openai.com/v1",
            model=config.model,
            extra_headers=config.extra.get("headers", {}),
            vision_capable=bool(config.extra.get("vision_capable", False)),
        )
    raise LLMUnavailableError(f"unknown LLM provider: {config.provider!r}")


@dataclass
class _NoneLLMClient(LLMClient):
    """Sentinel client for `provider="none"`. Always unavailable."""

    @property
    def provider_name(self) -> str:
        return "none"

    def available(self) -> bool:
        return False

    async def complete(
        self,
        *,
        system: str,
        user: str,
        timeout_s: float,
        cancellation: CancellationManager,
        **kwargs: Any,
    ) -> LLMResponse:
        raise LLMUnavailableError("LLM provider is 'none' (deterministic planner)")


# ===========================================================================
# Subprocess helper
# ===========================================================================


async def _run_subprocess(
    cmd: list[str], *, env: dict[str, str]
) -> Any:
    """Run a subprocess to completion and capture its output.

    Returns a lightweight namespace with `returncode`, `stdout`, `stderr`.
    The caller wraps the awaitable with `cancellation.wait_or_cancel(..., timeout=...)`.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()

    class _Result:
        __slots__ = ("returncode", "stdout", "stderr")

    r = _Result()
    r.returncode = proc.returncode
    r.stdout = stdout or b""
    r.stderr = stderr or b""
    return r


def _extract_openai_content(stdout: str) -> str:
    """Parse the z-ai CLI stdout.

    The CLI emits status lines like '🚀 Initializing Z-AI SDK...' followed
    by a JSON object with a `choices` array (OpenAI-compatible).
    """
    if not stdout:
        return ""
    idx = stdout.find("{")
    while idx != -1:
        candidate = stdout[idx:]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            end = candidate.rfind("}")
            if end != -1:
                try:
                    data = json.loads(candidate[: end + 1])
                except json.JSONDecodeError:
                    idx = stdout.find("{", idx + 1)
                    continue
            else:
                idx = stdout.find("{", idx + 1)
                continue
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str) and content:
                return content
        idx = stdout.find("{", idx + 1)
    return ""


__all__ = [
    "LLMClient",
    "LLMConfig",
    "LLMResponse",
    "LLMError",
    "LLMUnavailableError",
    "LLMTimeoutError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMResponseError",
    "ZaiLLMClient",
    "OpenAICompatLLMClient",
    "MockLLMClient",
    "make_llm_client",
]
