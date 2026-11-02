# language: Python, file: ai_solver.py
# AI-powered hCaptcha solver using Google Gemini Flash vision.
# Pipeline: open page → detect challenge → screenshot tiles → classify with Gemini → click matches → extract token

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright, Page, Locator

from config import config

logger = logging.getLogger("ai_solver")

# ─────────────────────────── constants ───────────────────────────────────────
TILE_SELECTOR = ".task-image"      # hCaptcha tile images
PROMPT_SELECTOR = ".prompt-text"   # challenge prompt text
SUBMIT_SELECTOR = ".button-submit" # submit button inside challenge
CHECKBOX_SELECTOR = "#checkbox"    # initial checkbox iframe

SOLVE_RETRIES = 5
TILE_SCREENSHOT_TIMEOUT = 5_000    # ms

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)


# ─────────────────────────── data types ──────────────────────────────────────
@dataclass
class TileResult:
    index: int
    image_b64: str
    matches: bool
    confidence: str = "unknown"
    raw_response: str = ""


@dataclass
class SolveAttempt:
    attempt: int
    prompt: str
    tiles_total: int
    tiles_clicked: int
    success: bool
    token: Optional[str] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0
    tile_results: list[TileResult] = field(default_factory=list)


@dataclass
class SolveResult:
    success: bool
    token: Optional[str]
    attempts: list[SolveAttempt]
    total_elapsed_s: float
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "token": self.token,
            "total_elapsed_s": self.total_elapsed_s,
            "error": self.error,
            "attempts": [
                {
                    "attempt": a.attempt,
                    "prompt": a.prompt,
                    "tiles_total": a.tiles_total,
                    "tiles_clicked": a.tiles_clicked,
                    "success": a.success,
                    "token": a.token,
                    "error": a.error,
                    "elapsed_s": a.elapsed_s,
                }
                for a in self.attempts
            ],
        }


def _clean_prompt_target(prompt: str) -> str:
    cleaned = prompt.strip()
    prefixes = [
        r"^please\s+click\s+each\s+image\s+containing\s+(an?|the)?\s*",
        r"^please\s+click\s+(all\s+)?(images?\s+with\s+)?(an?|the)?\s*",
        r"^please\s+tap\s+each\s+image\s+containing\s+(an?|the)?\s*",
        r"^tap\s+(on\s+)?(all\s+)?(the\s+)?",
        r"^click\s+(on\s+)?(all\s+)?(the\s+)?",
        r"^select\s+(all\s+)?(images?\s+with\s+)?(the\s+)?",
    ]
    for pattern in prefixes:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else prompt



class GeminiVisionClient:
    """High-speed, high-resolution vision client using Google Gemini Flash API with automatic key fallback."""

    def __init__(self, api_keys: str | list[str]):
        if isinstance(api_keys, list):
            self.api_keys = [k.strip() for k in api_keys if k and k.strip()]
        elif isinstance(api_keys, str):
            self.api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        else:
            self.api_keys = []
        self.current_key_index = 0
        self._session = None

    @property
    def api_key(self) -> str:
        """Returns the currently active API key."""
        if not self.api_keys:
            return ""
        return self.api_keys[self.current_key_index % len(self.api_keys)]

    async def is_available(self) -> bool:
        return any(len(k) > 10 for k in self.api_keys)

    async def get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=60)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def classify_grid(
        self,
        tile_images: list[tuple[int, Locator, Optional[str]]],
        prompt: str,
        example_img_b64: Optional[str] = None,
    ) -> list[TileResult]:
        """
        Classifies all 9 tiles simultaneously in high-resolution (220x220 per tile).
        Supports reference/example images and automatic zero-match rescue.
        """
        import aiohttp
        clean_target = _clean_prompt_target(prompt)

        # High-resolution grid: 220x220 per tile provides sharp, clean detail for the AI model
        tile_w, tile_h = 220, 220
        cols = 3 if len(tile_images) >= 9 else 2
        rows = (len(tile_images) + cols - 1) // cols
        grid = Image.new("RGB", (tile_w * cols, tile_h * rows), color=(15, 15, 18))
        draw = ImageDraw.Draw(grid)

        for i, _, img_b64 in tile_images:
            r, c = divmod(i, cols)
            x, y = c * tile_w, r * tile_h
            if img_b64:
                try:
                    img_data = base64.b64decode(img_b64)
                    tile_img = Image.open(io.BytesIO(img_data)).convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                    grid.paste(tile_img, (x, y))
                except Exception as exc:
                    logger.warning("Error pasting tile %d: %s", i, exc)
            # Distinct outline and non-intrusive corner number badge
            draw.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], outline=(255, 215, 0), width=2)
            draw.rectangle([x + 2, y + 2, x + 30, y + 28], fill=(0, 0, 0))
            draw.rectangle([x + 2, y + 2, x + 30, y + 28], outline=(255, 215, 0), width=1)
            draw.text((x + 9, y + 6), str(i), fill=(255, 255, 255))

        buf = io.BytesIO()
        grid.save(buf, format="JPEG", quality=92)
        grid_b64 = base64.b64encode(buf.getvalue()).decode()

        prompt_text = (
            f"You are an expert visual CAPTCHA verification model.\n"
            f"Challenge instruction: '{prompt}' (Target: '{clean_target}').\n"
            + ("Note: A reference/example item is provided above for comparison.\n" if example_img_b64 else "")
            + f"The main image shows a grid of tiles numbered 0 to {len(tile_images)-1} with a badge in the top-left of each tile.\n"
            f"Instructions:\n"
            f"1. Carefully examine each tile.\n"
            f"2. Identify every tile that satisfies the challenge instruction '{prompt}'.\n"
            f"3. Return ONLY a valid JSON array of matching integer tile numbers, for example: [1, 4, 7]. If none match, return []."
        )

        parts = []
        if example_img_b64:
            parts.append({"text": "Reference / Example item shown in challenge header:"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": example_img_b64}})
        parts.append({"text": prompt_text})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": grid_b64}})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.0, "response_mime_type": "application/json"}
        }

        num_keys = len(self.api_keys)
        if num_keys == 0:
            return [TileResult(index=i, image_b64=img or "", matches=False, raw_response="no_gemini_keys") for i, _, img in tile_images]

        last_error = "unknown_error"
        session = await self.get_session()

        for attempt in range(num_keys):
            active_key = self.api_keys[self.current_key_index % num_keys]
            key_preview = f"{active_key[:8]}...{active_key[-4:]}"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={active_key}"

            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            return [TileResult(index=i, image_b64=img or "", matches=False, raw_response="no_candidates") for i, _, img in tile_images]

                        content_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")
                        clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", content_text.strip(), flags=re.MULTILINE).strip()
                        try:
                            matched_indices = set(json.loads(clean_json))
                        except Exception:
                            nums = [int(n) for n in re.findall(r"\d+", clean_json)]
                            matched_indices = set(nums)

                        # Secondary rescue pass if 0 matches returned
                        if len(matched_indices) == 0:
                            logger.info("First pass returned 0 matches; querying secondary rescue prompt...")
                            rescue_parts = list(parts)
                            rescue_parts.append({"text": (
                                f"Notice: In this CAPTCHA challenge, at least one tile ALWAYS matches. "
                                f"Carefully re-evaluate the grid for '{clean_target}'. Which tile or tiles are the most likely or closest matches? "
                                f"Output strictly a JSON array of matching integer indices, e.g. [2]."
                            )})
                            try:
                                async with session.post(
                                    url,
                                    json={"contents": [{"parts": rescue_parts}], "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}},
                                    timeout=aiohttp.ClientTimeout(total=10)
                                ) as r2:
                                    if r2.status == 200:
                                        d2 = await r2.json()
                                        c2 = d2.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")
                                        clean_c2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", c2.strip(), flags=re.MULTILINE).strip()
                                        nums2 = [int(n) for n in re.findall(r"\d+", clean_c2)]
                                        if nums2:
                                            matched_indices = set(nums2)
                                            logger.info("Secondary rescue prompt found matches: %s", sorted(matched_indices))
                            except Exception as e2:
                                logger.debug("Secondary rescue prompt exception: %s", e2)

                        logger.info("Gemini [%s] classified grid for %r -> matches: %s", key_preview, clean_target, sorted(matched_indices))

                        results = []
                        for i, _, img_b64 in tile_images:
                            is_match = i in matched_indices
                            results.append(TileResult(
                                index=i,
                                image_b64=img_b64 or "",
                                matches=is_match,
                                confidence="high" if is_match else "low",
                                raw_response=content_text
                            ))
                        return results

                    err_text = await resp.text()
                    is_rate_limited = (
                        resp.status == 429
                        or resp.status == 403
                        or "RESOURCE_EXHAUSTED" in err_text
                        or "quota" in err_text.lower()
                        or "ratelimit" in err_text.lower()
                    )

                    if is_rate_limited:
                        logger.warning(
                            "Gemini key [%s] rate-limited / quota exhausted (HTTP %d). %s",
                            key_preview, resp.status,
                            "Falling back to next key..." if attempt < num_keys - 1 else "No more keys available."
                        )
                    else:
                        logger.warning(
                            "Gemini API HTTP %d with key [%s]: %s. %s",
                            resp.status, key_preview, err_text[:160],
                            "Trying fallback key..." if attempt < num_keys - 1 else ""
                        )

                    last_error = f"HTTP {resp.status}: {err_text[:100]}"
                    # Rotate key
                    self.current_key_index = (self.current_key_index + 1) % num_keys

            except Exception as exc:
                exc_desc = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                logger.warning("Gemini request failed with key [%s]: %s", key_preview, exc_desc)
                last_error = exc_desc
                self.current_key_index = (self.current_key_index + 1) % num_keys

        logger.error("All %d Gemini API keys failed or were rate-limited. Last error: %s", num_keys, last_error)
        return [TileResult(index=i, image_b64=img or "", matches=False, raw_response=last_error) for i, _, img in tile_images]



# ─────────────────────────── screenshot helpers ───────────────────────────────
async def _screenshot_element_b64(element: Locator) -> Optional[str]:
    """Screenshot a single element and return base64-encoded JPEG."""
    try:
        raw = await element.screenshot(type="jpeg", quality=80, timeout=TILE_SCREENSHOT_TIMEOUT)
        return base64.b64encode(raw).decode()
    except Exception as exc:
        logger.warning("Failed to screenshot element: %s", exc)
        return None


async def _get_challenge_prompt(frame) -> str:
    """Extract the text prompt from the hCaptcha challenge frame."""
    try:
        el = frame.locator(PROMPT_SELECTOR).first
        await el.wait_for(timeout=5_000)
        text = (await el.inner_text()).strip()
        return text
    except Exception:
        return "the object shown"


async def _get_example_image_b64(frame) -> Optional[str]:
    """Capture challenge reference / example image if present in the header."""
    try:
        ex = frame.locator(".example-wrapper, .example-image, .examples img, .challenge-example")
        count = await ex.count()
        for i in range(count):
            el = ex.nth(i)
            if await el.is_visible():
                img = await _screenshot_element_b64(el)
                if img:
                    return img
    except Exception as exc:
        logger.debug("No example image extracted: %s", exc)
    return None



async def _find_challenge_frame(page: Page, timeout_s: float = 6.0):
    """Return the challenge iframe's frame object, or None."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        for frame in page.frames:
            if "challenge" in frame.url or "hcaptcha.com/challenge" in frame.url:
                return frame
        await asyncio.sleep(0.2)
    return None


async def _click_checkbox(page: Page) -> bool:
    """Click the initial hCaptcha checkbox to trigger the challenge."""
    for frame in page.frames:
        if "hcaptcha.com/captcha" in frame.url or "newassets" in frame.url:
            try:
                cb = frame.locator(CHECKBOX_SELECTOR)
                if await cb.count() > 0:
                    await cb.click(force=True, timeout=5_000)
                    logger.info("Clicked hCaptcha checkbox")
                    return True
            except Exception as exc:
                logger.debug("Checkbox click failed on frame %s: %s", frame.url, exc)
    return False


async def _extract_token(page: Page) -> Optional[str]:
    """Extract hcaptcha-response token from the page."""
    # Strategy 1: hidden textarea
    try:
        token = await page.evaluate(
            "() => document.querySelector('[name=\"h-captcha-response\"]')?.value || "
            "document.querySelector('[name=\"g-recaptcha-response\"]')?.value || ''"
        )
        if token and len(token) > 20:
            return token
    except Exception:
        pass

    # Strategy 2: look in all iframes
    for frame in page.frames:
        try:
            token = await frame.evaluate(
                "() => document.querySelector('[name=\"h-captcha-response\"]')?.value || ''"
            )
            if token and len(token) > 20:
                return token
        except Exception:
            pass

    return None


# ─────────────────────────── core solver ─────────────────────────────────────
class HCaptchaAISolver:
    """
    AI-powered hCaptcha solver.

    Usage:
        solver = HCaptchaAISolver()
        result = await solver.solve("https://nopecha.com/demo/hcaptcha")
    """

    def __init__(
        self,
        max_retries: int = SOLVE_RETRIES,
        headless: bool = True,
        gemini_api_key: Optional[str | list[str]] = None,
    ):
        keys: list[str] = []
        if gemini_api_key:
            if isinstance(gemini_api_key, list):
                keys.extend([k.strip() for k in gemini_api_key if k and k.strip()])
            elif isinstance(gemini_api_key, str):
                keys.extend([k.strip() for k in gemini_api_key.split(",") if k.strip()])

        if not keys:
            if hasattr(config, "get_gemini_keys"):
                keys.extend(config.get_gemini_keys())
            elif getattr(config, "GEMINI_API_KEYS", None):
                keys.extend(config.GEMINI_API_KEYS)
            elif getattr(config, "GEMINI_API_KEY", ""):
                keys.extend([k.strip() for k in config.GEMINI_API_KEY.split(",") if k.strip()])

        # Deduplicate while preserving order
        unique_keys = []
        for k in keys:
            if k and k not in unique_keys:
                unique_keys.append(k)

        self.ai = GeminiVisionClient(unique_keys)
        self.max_retries = max_retries
        self.headless = headless

    async def solve(self, url: str) -> SolveResult:
        """Main entry point - open URL, solve hCaptcha, return result."""
        total_start = time.perf_counter()
        attempts: list[SolveAttempt] = []

        # Verify AI provider is reachable
        if not await self.ai.is_available():
            return SolveResult(
                success=False,
                token=None,
                attempts=[],
                total_elapsed_s=0,
                error="GEMINI_API_KEY is missing or invalid. Please configure it in .env or pass --gemini-key.",
            )

        key_count = len(getattr(self.ai, "api_keys", []))
        logger.info(
            "Using Google Gemini Flash cloud vision (%d key%s configured with automatic fallback)",
            key_count,
            "s" if key_count > 1 else "",
        )

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            try:
                logger.info("Navigating to %s", url)
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                await page.wait_for_timeout(1_000)

                # Click the initial checkbox
                clicked = await _click_checkbox(page)
                if not clicked:
                    logger.warning("Could not find/click checkbox - page may already show challenge")

                await page.wait_for_timeout(1_000)

                for attempt_no in range(1, self.max_retries + 1):
                    attempt_start = time.perf_counter()
                    logger.info("Solve attempt %d/%d", attempt_no, self.max_retries)

                    attempt = await self._solve_one_round(page, attempt_no)
                    attempt.elapsed_s = round(time.perf_counter() - attempt_start, 3)
                    attempts.append(attempt)

                    if attempt.success and attempt.token:
                        logger.info("Solved on attempt %d - token length %d", attempt_no, len(attempt.token))
                        return SolveResult(
                            success=True,
                            token=attempt.token,
                            attempts=attempts,
                            total_elapsed_s=round(time.perf_counter() - total_start, 3),
                        )

                    # Wait before retry
                    if attempt_no < self.max_retries:
                        await page.wait_for_timeout(1_000)

            except Exception as exc:
                logger.exception("Unexpected error during solve: %s", exc)
                return SolveResult(
                    success=False,
                    token=None,
                    attempts=attempts,
                    total_elapsed_s=round(time.perf_counter() - total_start, 3),
                    error=str(exc),
                )
            finally:
                if hasattr(self.ai, "close"):
                    await self.ai.close()
                await browser.close()

        return SolveResult(
            success=False,
            token=None,
            attempts=attempts,
            total_elapsed_s=round(time.perf_counter() - total_start, 3),
            error=f"Failed after {self.max_retries} attempts",
        )

    async def _solve_one_round(self, page: Page, attempt_no: int) -> SolveAttempt:
        """Attempt one round of challenge solving."""
        # Find challenge frame
        challenge_frame = await _find_challenge_frame(page)
        if challenge_frame is None:
            # Maybe already solved
            token = await _extract_token(page)
            if token:
                return SolveAttempt(
                    attempt=attempt_no,
                    prompt="(pre-solved)",
                    tiles_total=0,
                    tiles_clicked=0,
                    success=True,
                    token=token,
                )
            return SolveAttempt(
                attempt=attempt_no,
                prompt="",
                tiles_total=0,
                tiles_clicked=0,
                success=False,
                error="No challenge frame found",
            )

        # Get prompt text
        prompt = await _get_challenge_prompt(challenge_frame)
        logger.info("Challenge prompt: %r", prompt)

        # Find tiles
        tiles = challenge_frame.locator(TILE_SELECTOR)
        tile_count = await tiles.count()
        logger.info("Found %d tiles", tile_count)

        if tile_count == 0:
            return SolveAttempt(
                attempt=attempt_no,
                prompt=prompt,
                tiles_total=0,
                tiles_clicked=0,
                success=False,
                error="No tiles found in challenge frame",
            )

        # Concurrently screenshot each tile in parallel for speed
        async def _capture_tile(idx: int):
            t = tiles.nth(idx)
            img = await _screenshot_element_b64(t)
            return (idx, t, img)

        tile_images = await asyncio.gather(*(_capture_tile(i) for i in range(tile_count)))

        # Local vision classification helper
        # Check for challenge reference / example image
        example_img_b64 = await _get_example_image_b64(challenge_frame)
        if example_img_b64:
            logger.info("Challenge includes a reference/example image")

        tile_results = await self.ai.classify_grid(tile_images, prompt, example_img_b64=example_img_b64)

        # Click matching tiles with humanized offset and natural jitter
        clicked_count = 0
        for tr in tile_results:
            if tr.matches:
                tile = tiles.nth(tr.index)
                try:
                    box = await tile.bounding_box()
                    if box:
                        offset_x = box["width"] * random.uniform(0.35, 0.65)
                        offset_y = box["height"] * random.uniform(0.35, 0.65)
                        await page.mouse.click(box["x"] + offset_x, box["y"] + offset_y)
                    else:
                        await tile.click()
                    clicked_count += 1
                    logger.info("Clicked tile %d (confidence=%s)", tr.index, tr.confidence)
                    await page.wait_for_timeout(random.randint(120, 240))
                except Exception as exc:
                    logger.warning("Failed to click tile %d: %s", tr.index, exc)

        logger.info("Clicked %d/%d tiles", clicked_count, tile_count)

        # Submit the challenge if tiles were clicked, or refresh if none matched
        if clicked_count > 0:
            try:
                submit_btn = challenge_frame.locator(SUBMIT_SELECTOR).first
                await submit_btn.wait_for(state="visible", timeout=3_000)
                await submit_btn.click()
                logger.info("Clicked submit button")
            except Exception as exc:
                logger.warning("Submit button click failed: %s", exc)
        else:
            logger.warning("No matching tiles clicked; refreshing challenge")
            try:
                refresh_btn = challenge_frame.locator(".reload-button, .refresh-button, [title*='Refresh'], [title*='reload']").first
                if await refresh_btn.count() > 0:
                    await refresh_btn.click()
                    logger.info("Clicked refresh button")
            except Exception:
                pass

        # Reactively poll for token extraction or round progression (saves ~2 seconds)
        token = None
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            token = await _extract_token(page)
            if token and len(token) > 20:
                break
            await asyncio.sleep(0.15)

        success = bool(token and len(token) > 20)

        return SolveAttempt(
            attempt=attempt_no,
            prompt=prompt,
            tiles_total=tile_count,
            tiles_clicked=clicked_count,
            success=success,
            token=token,
            tile_results=tile_results,
        )


# ─────────────────────────── CLI entry point ─────────────────────────────────
async def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="AI hCaptcha Solver (Google Gemini Vision)")
    parser.add_argument("--url", default=config.DEMO_URL, help="URL with hCaptcha to solve")
    parser.add_argument("--retries", type=int, default=SOLVE_RETRIES, help="Max solve attempts")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window")
    parser.add_argument("--verify", action="store_true", help="Verify token via hCaptcha API")
    parser.add_argument("--save-report", action="store_true", help="Save result to reports/")
    parser.add_argument("--gemini-key", default="", help="Google Gemini API key (or comma-separated keys)")
    args = parser.parse_args()

    solver = HCaptchaAISolver(
        max_retries=args.retries,
        headless=not args.no_headless,
        gemini_api_key=args.gemini_key or None,
    )

    print(f"\n{'─'*55}")
    print(f"  AI hCaptcha Solver (Gemini Flash)")
    print(f"  URL    : {args.url}")
    print(f"  Retries: {args.retries}")
    print(f"{'─'*55}\n")

    result = await solver.solve(args.url)

    if result.success:
        print(f"SOLVED in {result.total_elapsed_s}s")
        print(f"   Token: {result.token[:60]}...")
    else:
        print(f"FAILED in {result.total_elapsed_s}s")
        print(f"   Error: {result.error}")

    if args.verify and result.token:
        from captcha_service import verify_token
        vr = verify_token(result.token)
        print(f"\n  Server verify: {'valid' if vr.success else 'invalid'}")
        print(f"  Raw: {vr.raw}")

    if args.save_report:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORTS_DIR / f"ai_solve_{ts}.json"
        report_path.write_text(json.dumps(result.to_dict(), indent=2))
        print(f"\n  Report saved: {report_path}")

    print()
    return result


if __name__ == "__main__":
    asyncio.run(main())
