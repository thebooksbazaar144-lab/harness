"""Environment-driven configuration.

All configuration is read from environment variables (optionally a `.env`
file). No hardcoded paths.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_session_root() -> Path:
    # Don't rely on CWD. Use an explicit, predictable, project-scoped root.
    return Path("/tmp/sera-browser-agent/sessions")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SBA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Browser
    browser_mode: Literal["auto", "headed", "headless"] = "auto"
    default_timeout_ms: int = 30_000
    navigation_timeout_ms: int = 45_000
    network_idle_timeout_ms: int = 8_000
    # v4: ONE bounded action budget. The old 15s default + sequential
    # fallback paths produced ~30s stuck clicks. 8s + a single 3s retry
    # bounds any failed action to ~11-12s worst case.
    action_timeout_ms: int = 8_000
    # v4: the ONLY retry's (tight) budget, applied after a fresh
    # re-resolution. Never another full action_timeout_ms.
    action_retry_timeout_ms: int = 3_000

    # Task limits
    max_steps: int = 40
    max_task_duration_s: int = 300

    # Loop detection (v4: tightened — 2 repeats instead of 4).
    tool_repeat_limit: int = 2
    semantic_loop_limit: int = 3
    # Same (action, target) failing this many times blocks the target.
    failure_block_limit: int = 2

    # Security
    local_lab: bool = False
    allowed_domains: str = ""
    allowed_private_targets: str = ""
    allow_js_eval: bool = False
    log_sensitive: bool = False

    # Fallback
    enable_browser_use_fallback: bool = False

    # LLM planner (provider-agnostic)
    # none    → deterministic planner only (default; zero LLM dependency)
    # zai     → use the `z-ai` CLI as the LLM provider
    # openai  → use any OpenAI-compatible REST API (OpenAI, Azure, OpenRouter,
    #           Together, Anyscale, vLLM, Ollama, LM Studio, ...)
    llm_provider: Literal["none", "zai", "openai"] = "none"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout_s: float = 30.0
    # Whether the configured model accepts image inputs (vision escalation).
    # Vision is NEVER sent on every step — only when the planner or challenge
    # detector explicitly requests it.
    llm_vision_capable: bool = False
    # Disable LLM planner after this many consecutive fallbacks (safety valve).
    llm_max_consecutive_fallbacks: int = 5
    # v4 vision budget: hard cap on vision calls per task.
    vision_max_calls: int = 2
    # v4 vision circuit breaker: consecutive vision provider failures that
    # disable vision for the rest of the task. Provider errors must never
    # become an exploration strategy.
    vision_failure_limit: int = 2

    # Advanced features
    # Observability: Playwright traces + structured action log
    enable_tracing: bool = True
    enable_action_log: bool = True
    # Memory: long_term_memory + rolling history buffer
    enable_memory: bool = True
    memory_max_full_steps: int = 40
    # Snapshot trimming cap (chars)
    snapshot_max_chars: int = 50_000
    # Safety: block dangerous-keyword actions in autonomous mode
    safety_block_dangerous: bool = True
    safety_action_policy: Literal["readonly", "full", "custom"] = "full"
    # Multi-tab support
    enable_multi_tab: bool = True
    # Element resolver timeout (ms)
    resolver_timeout_ms: int = 5000
    # flash_mode: skip per-step reflection for routine actions
    flash_mode: bool = False

    # Stealth layer (anti-bot fingerprint evasion). off | light | medium | aggressive
    stealth_level: Literal["off", "light", "medium", "aggressive"] = "off"
    # Fixed stealth values (override randomization). Empty = randomized.
    stealth_user_agent: str = ""
    stealth_viewport: str = ""  # "WxH" e.g. "1920x1080"
    stealth_timezone: str = ""
    stealth_locale: str = ""
    # Proxy URL for stealth (e.g. "http://user:pass@host:port"). Empty = no proxy.
    stealth_proxy: str = ""

    # Multi-agent pattern: none | plan_act_validate
    agent_pattern: Literal["none", "plan_act_validate"] = "none"

    # Snapshot method: dom | cdp | auto
    # dom = DOM-based walker (fast, always works, less ARIA detail)
    # cdp = CDP Accessibility.getFullAXTree (richer, pierces shadow DOM, Chromium-only)
    # auto = try CDP first, fall back to DOM (default; may add latency)
    snapshot_method: Literal["dom", "cdp", "auto"] = "dom"

    # Real-view browser viewer (human-takeoverable browser for SERA chat UI)
    # When enabled, the browser launches in headed mode with CDP remote debugging,
    # and the agent streams screenshots + state to connected WebSocket clients.
    # The human can watch every step and grab control at any time.
    enable_viewer: bool = False
    # CDP remote debugging port (0 = random; the effective port is exposed in status).
    viewer_cdp_port: int = 9222
    # Stream screenshots after each step (JPEG, smaller than PNG).
    viewer_stream_screenshots: bool = True
    # Screenshot quality (1-100, JPEG only).
    viewer_screenshot_quality: int = 60
    # Screenshot max width (downscaled for faster streaming).
    viewer_screenshot_max_width: int = 1280
    # Human takeover timeout in seconds (how long the agent waits for the human).
    viewer_takeover_timeout_s: int = 300
    # Force headed mode when viewer is enabled (overrides browser_mode).
    viewer_force_headed: bool = True

    # API
    api_host: str = "127.0.0.1"
    api_port: int = 8765

    # v4: browser backend selection.
    #   local  → LocalPlaywrightRuntime (default; fully supported)
    #   kernel → KernelRuntime (remote kernel browser service)
    #   auto   → kernel when SBA_KERNEL_URL is set and healthy, else local
    browser_backend: Literal["auto", "local", "kernel"] = "auto"
    # Base URL of the kernel browser service (e.g. http://127.0.0.1:9223).
    # Empty → kernel backend unavailable; auto falls back to local.
    kernel_url: str = ""
    kernel_timeout_s: float = 10.0

    # Storage
    session_root: Path = Field(default_factory=_default_session_root)
    screenshot_root: Path = Path("/tmp/sera-browser-agent/screenshots")

    # Logging
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("session_root", "screenshot_root", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @property
    def allowed_domains_list(self) -> list[str]:
        return [d.strip() for d in self.allowed_domains.split(",") if d.strip()]

    @property
    def allowed_private_targets_list(self) -> list[str]:
        return [
            t.strip() for t in self.allowed_private_targets.split(",") if t.strip()
        ]

    def effective_browser_mode(self, *, has_display: bool | None = None) -> str:
        if self.browser_mode != "auto":
            return self.browser_mode
        if has_display is None:
            has_display = bool(os.environ.get("DISPLAY")) or bool(
                os.environ.get("WAYLAND_DISPLAY")
            )
        return "headed" if has_display else "headless"

    def to_llm_config(self) -> "LLMConfig":
        """Build a provider-agnostic LLMConfig from these settings.

        `LLMConfig` lives in `task/llm_client.py`; we import lazily to avoid
        a config → task circular import.
        """
        from .task.llm_client import LLMConfig

        return LLMConfig(
            provider=self.llm_provider,
            model=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            timeout_s=self.llm_timeout_s,
            extra={"vision_capable": self.llm_vision_capable},
        )

    def to_stealth_config(self) -> "StealthConfig":
        """Build a StealthConfig from these settings.

        Parses `stealth_viewport` ("WxH") into a dict. Returns a StealthConfig
        with `level="off"` if stealth is disabled.
        """
        from .browser.stealth import StealthConfig

        viewport: dict[str, int] | None = None
        if self.stealth_viewport:
            try:
                w, h = self.stealth_viewport.lower().split("x")
                viewport = {"width": int(w), "height": int(h)}
            except (ValueError, AttributeError):
                pass
        return StealthConfig(
            level=self.stealth_level,
            user_agent=self.stealth_user_agent,
            viewport=viewport,
            timezone=self.stealth_timezone,
            locale=self.stealth_locale,
            proxy=self.stealth_proxy,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
