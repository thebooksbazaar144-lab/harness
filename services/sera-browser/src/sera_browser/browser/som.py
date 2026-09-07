"""Set-of-Mark (SoM) vision fallback.

Inspired by WebVoyager's SoM prompting and VisualWebArena. When the
accessibility tree is empty/misleading (canvas, custom-drawn UI, image
maps), we draw bounding-box + numeric-ID overlays on a screenshot and let
a VLM pick an ID instead of error-prone pixel coordinates.

This gives vision models a discrete, stable action vocabulary (IDs) — the
proven way to make vision reliable enough for production.

Flow:
1. Capture a screenshot.
2. Get the bounding boxes of all interactive elements (via CDP
   `DOM.getBoxModel` or Playwright's `element.bounding_box()`).
3. Draw a numbered rectangle + ID label on each element.
4. Send the annotated screenshot to the VLM with a prompt like
   "Click the element with ID 5".
5. The VLM returns an ID; we map it back to a Playwright element.

This is ONLY used as a vision-escalation fallback — never on every step.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError, Locator, Page

from ..logging import get_logger
from ..models import InteractiveElement

log = get_logger("sera.browser.som")


@dataclass
class MarkedElement:
    """An element annotated with a bounding box + numeric ID."""
    index: int
    element: InteractiveElement
    bbox: dict[str, float]  # {x, y, width, height}
    locator: Locator | None = None


@dataclass
class SoMResult:
    """The result of a Set-of-Mark annotation pass."""
    image_png: bytes
    marked_elements: list[MarkedElement] = field(default_factory=list)
    note: str = ""


class SetOfMarkAnnotator:
    """Annotates a screenshot with bounding-box + numeric-ID overlays.

    Uses Pillow (PIL) for drawing — a common, lightweight dependency. If PIL
    is not installed, falls back to returning the unannotated screenshot
    + a text list of element IDs (no visual overlay).
    """

    def __init__(self) -> None:
        self._has_pil = self._check_pil()

    @staticmethod
    def _check_pil() -> bool:
        try:
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401

            return True
        except ImportError:
            return False

    async def annotate(
        self,
        page: Page,
        elements: list[InteractiveElement],
        *,
        screenshot_png: bytes | None = None,
    ) -> SoMResult:
        """Annotate a screenshot with SoM overlays.

        Args:
            page: The Playwright page.
            elements: Interactive elements to annotate (capped at ~30 for clarity).
            screenshot_png: Pre-captured screenshot. If None, captures one.

        Returns:
            SoMResult with the annotated image + marked_elements list.
        """
        # Capture screenshot if not provided.
        if screenshot_png is None:
            try:
                screenshot_png = await page.screenshot(full_page=False)
            except PlaywrightError as e:
                log.warning("SoM screenshot capture failed", extra={"event": "som.screenshot.fail", "err": str(e)})
                return SoMResult(image_png=b"", note="screenshot capture failed")

        if not screenshot_png:
            return SoMResult(image_png=b"", note="empty screenshot")

        # Cap elements to avoid cluttering the image.
        elements = elements[:30]

        # Get bounding boxes for each element.
        marked: list[MarkedElement] = []
        for i, el in enumerate(elements, 1):
            bbox = await self._get_bounding_box(page, el)
            if bbox is not None:
                marked.append(
                    MarkedElement(
                        index=i,
                        element=el,
                        bbox=bbox,
                    )
                )

        if not marked:
            return SoMResult(
                image_png=screenshot_png,
                marked_elements=[],
                note="no interactive elements with bounding boxes",
            )

        # Draw overlays if PIL is available.
        if self._has_pil:
            annotated = self._draw_overlays(screenshot_png, marked)
            note = f"Annotated {len(marked)} elements with numeric IDs."
        else:
            # Fallback: return the raw screenshot + a text description.
            annotated = screenshot_png
            note = (
                f"PIL not installed; returning raw screenshot. "
                f"Elements (ID → ref): "
                + ", ".join(f"{m.index}={m.element.ref}" for m in marked[:10])
            )

        return SoMResult(
            image_png=annotated,
            marked_elements=marked,
            note=note,
        )

    async def _get_bounding_box(
        self, page: Page, el: InteractiveElement
    ) -> dict[str, float] | None:
        """Get the bounding box of an element via Playwright."""
        try:
            locator = self._make_locator(page, el)
            if locator is None:
                return None
            bbox = await locator.bounding_box()
            if bbox is None:
                return None
            return {
                "x": float(bbox.get("x", 0)),
                "y": float(bbox.get("y", 0)),
                "width": float(bbox.get("width", 0)),
                "height": float(bbox.get("height", 0)),
            }
        except (PlaywrightError, Exception) as e:  # noqa: BLE001
            log.debug(
                "SoM bounding box failed",
                extra={"event": "som.bbox.fail", "ref": el.ref, "err": str(e)},
            )
            return None

    @staticmethod
    def _make_locator(page: Page, el: InteractiveElement) -> Locator | None:
        """Create a Playwright locator for an element."""
        try:
            if el.selector:
                return page.locator(el.selector).first
            if el.role and el.name:
                return page.get_by_role(el.role, name=el.name, exact=False).first
            if el.href:
                return page.locator(f"a[href='{el.href}']").first
        except PlaywrightError:
            return None
        return None

    def _draw_overlays(
        self, screenshot_png: bytes, marked: list[MarkedElement]
    ) -> bytes:
        """Draw numbered rectangles + ID labels on the screenshot."""
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(screenshot_png))
        draw = ImageDraw.Draw(img)
        for m in marked:
            x = m.bbox["x"]
            y = m.bbox["y"]
            w = m.bbox["width"]
            h = m.bbox["height"]
            # Draw a red rectangle around the element.
            draw.rectangle([x, y, x + w, y + h], outline="red", width=2)
            # Draw the ID label in the top-left corner.
            label = str(m.index)
            # Draw a small filled rectangle for the label background.
            label_w = max(len(label) * 8 + 6, 18)
            draw.rectangle([x, y - 16, x + label_w, y], fill="red")
            draw.text((x + 3, y - 14), label, fill="white")
        # Save back to PNG bytes.
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def resolve_id(self, som_result: SoMResult, element_id: int) -> MarkedElement | None:
        """Map a VLM-returned element ID back to a MarkedElement."""
        for m in som_result.marked_elements:
            if m.index == element_id:
                return m
        return None


__all__ = ["SetOfMarkAnnotator", "SoMResult", "MarkedElement"]
