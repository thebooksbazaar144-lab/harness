"""ChallengeDetector — detect & classify CAPTCHA / bot protection / MFA.

For authorized testing only:
1. detect
2. classify
3. record in BrowserState
4. check official test mechanism availability
5. use official test config where available
6. else pause for human-in-loop
7. resume after legitimate completion

We do NOT implement techniques intended to bypass third-party
CAPTCHA / anti-bot protections.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError, Page

from ..logging import get_logger
from ..models import Challenge

log = get_logger("sera.challenge")


ChallengeType = Literal[
    "captcha",
    "cloudflare_turnstile",
    "cloudflare_interstitial",
    "hcaptcha",
    "recaptcha",
    "verify_human",
    "bot_detection",
    "login_challenge",
    "mfa",
    "unknown",
]


# Cloudflare Turnstile test keys (documented for automated testing).
# Sitekey: 1x00000000000000000000AA (always passes)
# Secret:  1x0000000000000000000000000000000AA
# Source: Cloudflare docs "Testing Turnstile".
TURNSTILE_TEST_SITEKEY = "1x00000000000000000000AA"
TURNSTILE_TEST_SECRET = "1x0000000000000000000000000000000AA"

# reCAPTCHA test keys (Google dev docs).
# Site key: 6LeIxAcTAAAAAJcZVRqyebecYbd0OXwI1eAO7TNJ (always passes)
# Secret:   6LeIxAcTAAAAAGG-vFI1TnRWcMZaa7gaPii5gpMN
RECAPTCHA_TEST_SITEKEY = "6LeIxAcTAAAAAJcZVRqyebecYbd0OXwI1eAO7TNJ"


_SELECTORS: list[tuple[str, ChallengeType, str]] = [
    # Cloudflare Turnstile
    ('iframe[src*="challenges.cloudflare.com"]', "cloudflare_turnstile", "Turnstile iframe"),
    ('div[class*="cf-turnstile"]', "cloudflare_turnstile", "cf-turnstile container"),
    # Cloudflare interstitial
    ('div[id="cf-challenge-running"]', "cloudflare_interstitial", "Cloudflare challenge page"),
    ('div[id="cf-spinner-please-wait"]', "cloudflare_interstitial", "Cloudflare please-wait"),
    ('div[id="challenge-running"]', "cloudflare_interstitial", "Cloudflare challenge running"),
    ('div[class*="cf-browser-verification"]', "cloudflare_interstitial", "Cloudflare browser verification"),
    # hCaptcha
    ('iframe[src*="hcaptcha.com"]', "hcaptcha", "hCaptcha iframe"),
    ('div[class*="h-captcha"]', "hcaptcha", "hCaptcha container"),
    # reCAPTCHA
    ('iframe[src*="recaptcha"]', "recaptcha", "reCAPTCHA iframe"),
    ('div[class*="g-recaptcha"]', "recaptcha", "g-recaptcha container"),
    ('div[class*="rc-anchor"]', "recaptcha", "rc-anchor"),
    # Generic CAPTCHA
    ('img[src*="captcha"]', "captcha", "captcha image"),
    ('input[name*="captcha"]', "captcha", "captcha input"),
    # Bot detection / verify-human interstitials
    ('div[class*="bot-detect"]', "bot_detection", "bot detection wall"),
    ('div[id*="bot-detect"]', "bot_detection", "bot detection wall"),
    # MFA / 2FA prompts
    ('input[name*="otp"]', "mfa", "OTP input"),
    ('input[name*="totp"]', "mfa", "TOTP input"),
    ('input[id*="mfa"]', "mfa", "MFA input"),
    ('input[autocomplete*="one-time-code"]', "mfa", "one-time-code input"),
]


_TEXT_PATTERNS: list[tuple[str, ChallengeType]] = [
    ("verify you are human", "verify_human"),
    ("verifying you are human", "verify_human"),
    ("checking your browser", "cloudflare_interstitial"),
    ("enable javascript and cookies", "cloudflare_interstitial"),
    ("please wait while we verify", "cloudflare_interstitial"),
    ("attention required", "bot_detection"),
    ("two-factor authentication", "mfa"),
    ("enter the code", "mfa"),
    ("two step verification", "mfa"),
]


@dataclass
class ChallengeDetectionResult:
    challenges: list[Challenge]
    has_login_form: bool
    login_url: str | None = None


class ChallengeDetector:
    """Detects & classifies challenges. Does NOT bypass protections.

    Two detection paths:
    1. Selector + text scan (existing) — checks known CSS selectors + body text.
    2. Page-context deep scan (dsh-browser inspired) — runs a JS expression
       that scans shadow roots + same-origin iframes for captcha markers,
       detecting Cloudflare/hCaptcha/reCAPTCHA/Turnstile/generic with
       bilingual (EN + 中文) wording. This catches challenges hidden inside
       shadow DOM that selector-based detection misses.
    """

    async def detect(self, page: Page) -> ChallengeDetectionResult:
        out: list[Challenge] = []

        # Selector-based detection
        for sel, kind, evidence in _SELECTORS:
            try:
                count = await page.locator(sel).count()
            except PlaywrightError:
                count = 0
            if count > 0:
                c = Challenge(type=kind, evidence=evidence, selector=sel)
                # Check for official test keys on owned Turnstile/reCAPTCHA.
                if kind == "cloudflare_turnstile":
                    handled, msg = await self._check_turnstile_test_key(page)
                    if handled:
                        c.handled = True
                        c.handling = "official_test_key"
                elif kind == "recaptcha":
                    handled, msg = await self._check_recaptcha_test_key(page)
                    if handled:
                        c.handled = True
                        c.handling = "official_test_key"
                out.append(c)

        # dsh-browser-inspired deep scan: shadow DOM + same-origin iframes.
        deep_result = await self._deep_scan(page)
        if deep_result is not None:
            kind, reason = deep_result
            # Only add if not already detected by selectors.
            if not any(c.type == kind for c in out):
                out.append(Challenge(type=kind, evidence=reason))

        # Text-based detection
        try:
            body_text = (await page.inner_text("body"))[:4000].lower()
        except PlaywrightError:
            body_text = ""
        for needle, kind in _TEXT_PATTERNS:
            if needle in body_text and not any(c.type == kind for c in out):
                out.append(Challenge(type=kind, evidence=f"text: {needle!r}"))

        # Login challenge detection
        has_login = False
        login_url = None
        try:
            pw = await page.locator('input[type="password"]').count()
        except PlaywrightError:
            pw = 0
        if pw > 0:
            has_login = True
            login_url = page.url
            # Only flag as challenge if we're not already logged in / and there's
            # no other higher-priority challenge pending.
            if not any(c.type == "login_challenge" for c in out):
                out.append(
                    Challenge(
                        type="login_challenge",
                        evidence="password input present",
                        selector='input[type="password"]',
                    )
                )

        for c in out:
            log.info(
                "challenge detected",
                extra={
                    "event": "challenge.detect",
                    "type": c.type,
                    "evidence": c.evidence,
                    "handled": c.handled,
                    "handling": c.handling,
                },
            )
        return ChallengeDetectionResult(challenges=out, has_login_form=has_login, login_url=login_url)

    async def _check_turnstile_test_key(self, page: Page) -> tuple[bool, str]:
        """If the page is using the Turnstile test sitekey, the challenge
        will pass automatically and we can proceed."""
        try:
            html = await page.content()
        except PlaywrightError:
            return False, ""
        if TURNSTILE_TEST_SITEKEY in html:
            log.info(
                "Turnstile official test sitekey detected",
                extra={"event": "challenge.turnstile.test_key", "sitekey": TURNSTILE_TEST_SITEKEY},
            )
            return True, "official_turnstile_test_sitekey"
        return False, ""

    async def _check_recaptcha_test_key(self, page: Page) -> tuple[bool, str]:
        try:
            html = await page.content()
        except PlaywrightError:
            return False, ""
        if RECAPTCHA_TEST_SITEKEY in html:
            log.info(
                "reCAPTCHA official test sitekey detected",
                extra={"event": "challenge.recaptcha.test_key", "sitekey": RECAPTCHA_TEST_SITEKEY},
            )
            return True, "official_recaptcha_test_sitekey"
        return False, ""

    async def _deep_scan(self, page: Page) -> tuple[str, str] | None:
        """dsh-browser-inspired deep scan: shadow DOM + same-origin iframes.

        Runs a page-context JS expression that scans shadow roots and
        same-origin iframes for captcha markers. Returns (kind, reason) or None.

        This catches challenges hidden inside shadow DOM that selector-based
        detection misses (modern captcha widgets often encapsulate their
        UI in closed shadow roots).
        """
        try:
            result = await page.evaluate("""(() => {
                const title = (document.title || '').trim();
                const bodyText = (document.body && document.body.innerText || '').slice(0, 4000);
                let extraText = '';
                try {
                    const seen = new Set();
                    const scan = (doc) => {
                        if (seen.has(doc)) return;
                        seen.add(doc);
                        for (const el of doc.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                extraText += (el.shadowRoot.textContent || '').slice(0, 2000);
                                scan(el.shadowRoot);
                            }
                            if (el.tagName === 'IFRAME') {
                                try {
                                    const d = el.contentDocument;
                                    if (d) {
                                        extraText += ((d.body && d.body.innerText) || '').slice(0, 2000);
                                        scan(d);
                                    }
                                } catch { /* cross-origin */ }
                            }
                        }
                    };
                    scan(document);
                } catch { /* never fail the challenge check */ }
                const lower = (title + '\\n' + bodyText + '\\n' + extraText).toLowerCase();
                const frameSrcs = [...document.querySelectorAll('iframe')].map(f => f.src || '').join(' ');
                const framesLower = frameSrcs.toLowerCase();
                const hasCfInterstitial = /just a moment|checking your browser|attention required|cf_chl/i.test(lower)
                    || !!document.querySelector('#challenge-running, #challenge-stage, #cf-chl-container');
                const hasHCaptcha = !!window.hcaptcha || !!document.querySelector('.h-captcha') || /hcaptcha\\.com/i.test(framesLower);
                const hasRecaptcha = !!window.grecaptcha || !!document.querySelector('.g-recaptcha') || /recaptcha\\/api|google\\.com\\/recaptcha/i.test(framesLower);
                const hasTurnstile = !!window.turnstile || /challenges\\.cloudflare\\.com/i.test(framesLower) || /turnstile|challenge-platform/i.test(lower);
                const verifyWording = /verify you are human|verify you are not a robot|人机验证|安全验证|enable javascript and cookies|请.*验证/i.test(lower);
                if (hasCfInterstitial) return { blocked: true, kind: 'cloudflare_interstitial', reason: 'Cloudflare "Just a moment" interstitial' };
                if (hasHCaptcha) return { blocked: true, kind: 'hcaptcha', reason: 'hCaptcha verification' };
                if (hasRecaptcha) return { blocked: true, kind: 'recaptcha', reason: 'Google reCAPTCHA verification' };
                if (hasTurnstile) return { blocked: true, kind: 'cloudflare_turnstile', reason: 'Cloudflare Turnstile verification' };
                if (verifyWording && /challenge|captcha|verification|security check|access denied|blocked|验证/i.test(lower)) {
                    return { blocked: true, kind: 'verify_human', reason: 'Human-verification challenge' };
                }
                return { blocked: false };
            })()""")
        except PlaywrightError:
            return None
        if not isinstance(result, dict) or not result.get("blocked"):
            return None
        kind = result.get("kind", "unknown")
        reason = result.get("reason", "human-verification challenge")
        # Map to our Challenge type enum.
        kind_map = {
            "cloudflare_interstitial": "cloudflare_interstitial",
            "hcaptcha": "hcaptcha",
            "recaptcha": "recaptcha",
            "cloudflare_turnstile": "cloudflare_turnstile",
            "verify_human": "verify_human",
        }
        return kind_map.get(kind, "unknown"), reason

    def requires_human(self, challenges: list[Challenge]) -> Challenge | None:
        """Return the first challenge that needs human-in-loop handling."""
        for c in challenges:
            if c.handled:
                continue
            if c.type in {"captcha", "cloudflare_turnstile", "cloudflare_interstitial", "hcaptcha", "recaptcha", "verify_human", "bot_detection", "mfa"}:
                return c
        return None
