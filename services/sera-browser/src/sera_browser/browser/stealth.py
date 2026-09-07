"""Pluggable stealth layer — anti-bot fingerprint evasion.

Inspired by playwright-stealth, feder-cr/invisible_playwright, and
fingerprint-suite. Many target sites block headless Playwright on sight;
without stealth the agent can't even load the page.

This layer is OPT-IN and pluggable. It does NOT bypass CAPTCHA or anti-bot
protections — it only reduces the automation fingerprint so legitimate
testing isn't blocked by naive detection.

Capabilities:
1. **User-agent randomization** — rotate from a pool of realistic UAs.
2. **Viewport randomization** — vary screen dimensions per context.
3. **Timezone + locale** — set to match the UA's expected region.
4. **WebGL / canvas fingerprint noise** — inject subtle noise via init_script.
5. **navigator.webdriver = false** — remove the automation flag.
6. **Extra headers** — add realistic Accept-Language, Sec-Ch-Ua, etc.
7. **Proxy rotation** — optional residential/datacenter proxy support.

Usage:
    stealth = StealthLayer(StealthConfig(level="medium"))
    await stealth.apply(context, page)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Literal

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from ..logging import get_logger

log = get_logger("sera.browser.stealth")


StealthLevel = Literal["off", "light", "medium", "aggressive"]


# Realistic desktop user agents (Chrome on Windows/Mac/Linux).
# These are rotated per-context to avoid trivial fingerprint stability.
_USER_AGENTS = [
    # Chrome 131 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Edge 131 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
]

_LOCALES = [
    "en-US",
    "en-GB",
    "en-CA",
]


# JavaScript init script that patches common automation-detection vectors.
# This is the "light" stealth — removes navigator.webdriver, patches
# plugins length, languages, and chrome runtime. For "medium" and
# "aggressive" we add canvas/WebGL noise.
_STEALTH_INIT_SCRIPT = """
() => {
    // Remove navigator.webdriver flag.
    try {
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    } catch (e) {}
    // Patch plugins to look non-empty.
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
    } catch (e) {}
    // Patch languages to look realistic.
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });
    } catch (e) {}
    // Add chrome runtime object (headless Chrome lacks it).
    try {
        window.chrome = window.chrome || { runtime: {} };
    } catch (e) {}
    // Patch permissions API.
    try {
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(parameters);
    } catch (e) {}
}
"""

# Canvas + WebGL fingerprint noise (medium/aggressive levels).
_CANVAS_NOISE_SCRIPT = """
() => {
    // Add subtle noise to canvas toDataURL / toBlob.
    try {
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(...args) {
            const ctx = this.getContext('2d');
            if (ctx) {
                // Add a single near-invisible pixel of noise.
                const imgData = ctx.getImageData(0, 0, 1, 1);
                imgData.data[0] = (imgData.data[0] + 1) % 256;
                ctx.putImageData(imgData, 0, 0);
            }
            return origToDataURL.apply(this, args);
        };
    } catch (e) {}
    // Spoof WebGL vendor/renderer.
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.'; // UNMASKED_VENDOR_WEBGL
            if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
            return getParameter.call(this, p);
        };
    } catch (e) {}
}
"""


@dataclass
class StealthConfig:
    """Stealth layer configuration."""
    level: StealthLevel = "off"
    # Fixed values (override randomization). If empty, randomized.
    user_agent: str = ""
    viewport: dict[str, int] | None = None
    timezone: str = ""
    locale: str = ""
    # Proxy URL (e.g. "http://user:pass@host:port"). Empty = no proxy.
    proxy: str = ""
    # Extra headers to inject.
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.level != "off"


@dataclass
class StealthFingerprint:
    """The chosen fingerprint for one context."""
    user_agent: str
    viewport: dict[str, int]
    timezone: str
    locale: str
    level: StealthLevel


class StealthLayer:
    """Applies stealth configuration to a Playwright context.

    Usage:
        stealth = StealthLayer(StealthConfig(level="medium"))
        fingerprint = stealth.choose_fingerprint()
        # Pass fingerprint to ContextManager.open() via user_agent/viewport.
        await stealth.apply(page, fingerprint)
    """

    def __init__(self, config: StealthConfig | None = None) -> None:
        self.config = config or StealthConfig()
        self._rng = random.Random()

    def choose_fingerprint(self) -> StealthFingerprint:
        """Pick a randomized fingerprint based on the configured level."""
        cfg = self.config
        ua = cfg.user_agent or self._rng.choice(_USER_AGENTS)
        vp = cfg.viewport or self._rng.choice(_VIEWPORTS)
        tz = cfg.timezone or self._rng.choice(_TIMEZONES)
        loc = cfg.locale or self._rng.choice(_LOCALES)
        return StealthFingerprint(
            user_agent=ua,
            viewport=vp,
            timezone=tz,
            locale=loc,
            level=cfg.level,
        )

    async def apply(self, page: Page, fingerprint: StealthFingerprint) -> None:
        """Apply stealth patches to a page (init scripts + headers).

        Call this AFTER the page is created but BEFORE navigating.
        """
        if not self.config.enabled:
            return
        try:
            # Inject the stealth init script.
            await page.add_init_script(_STEALTH_INIT_SCRIPT)
            if self.config.level in ("medium", "aggressive"):
                await page.add_init_script(_CANVAS_NOISE_SCRIPT)
            # Set extra headers.
            headers = {
                "Accept-Language": f"{fingerprint.locale},{fingerprint.locale.split('-')[0]};q=0.9",
            }
            headers.update(self.config.extra_headers)
            await page.set_extra_http_headers(headers)
            log.info(
                "stealth applied",
                extra={
                    "event": "stealth.apply",
                    "level": self.config.level,
                    "ua": fingerprint.user_agent[:60],
                    "viewport": fingerprint.viewport,
                    "timezone": fingerprint.timezone,
                },
            )
        except PlaywrightError as e:
            log.warning(
                "stealth apply failed",
                extra={"event": "stealth.apply.fail", "err": str(e)},
            )

    def context_kwargs(self, fingerprint: StealthFingerprint) -> dict[str, Any]:
        """Playwright context creation kwargs derived from the fingerprint.

        Pass these to `browser.new_context(**kwargs)`.
        """
        if not self.config.enabled:
            return {}
        kwargs: dict[str, Any] = {
            "user_agent": fingerprint.user_agent,
            "viewport": fingerprint.viewport,
            "timezone_id": fingerprint.timezone,
            "locale": fingerprint.locale,
            "extra_http_headers": {
                "Accept-Language": f"{fingerprint.locale},{fingerprint.locale.split('-')[0]};q=0.9",
            },
        }
        if self.config.proxy:
            proxy_url = self.config.proxy
            # Parse proxy URL.
            from urllib.parse import urlparse

            parsed = urlparse(proxy_url)
            proxy_kwargs: dict[str, Any] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_kwargs["username"] = parsed.username
            if parsed.password:
                proxy_kwargs["password"] = parsed.password
            kwargs["proxy"] = proxy_kwargs
        return kwargs


__all__ = [
    "StealthLayer",
    "StealthConfig",
    "StealthFingerprint",
    "StealthLevel",
]
