# language: Python, file: browser_test.py
# Playwright automation — renders hCaptcha widget, extracts test token.

import argparse
import asyncio
import sys
from typing import Optional
from playwright.async_api import async_playwright, Page, Browser
from config import config

DEFAULT_DEMO_URL = getattr(config, "DEMO_URL", "https://nopecha.com/demo/hcaptcha")


class BrowserSession:
    def __init__(self, headless: Optional[bool] = None):
        self._headless = config.BROWSER_HEADLESS if headless is None else headless
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        return await ctx.new_page()


async def extract_token_from_page(
    page: Page,
    url: str = DEFAULT_DEMO_URL,
    timeout_s: int = 60,
) -> Optional[str]:
    print(f"[browser_test] Navigating to: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        print(f"[browser_test] Warning during navigation: {exc}")

    # Wait for the hCaptcha checkbox frame
    print("[browser_test] Locating hCaptcha widget...")
    checkbox_frame = None
    for _ in range(30):
        for f in page.frames:
            if ("hcaptcha.com" in f.url or "newassets.hcaptcha.com" in f.url):
                try:
                    if await f.locator("#checkbox").count() > 0:
                        checkbox_frame = f
                        break
                except Exception:
                    pass
        if checkbox_frame:
            break
        await asyncio.sleep(0.5)

    if checkbox_frame is None:
        print("[browser_test] Error: Could not locate hCaptcha checkbox iframe.")
        return None

    print("[browser_test] Found hCaptcha checkbox widget. Clicking checkbox...")
    try:
        checkbox = checkbox_frame.locator("#checkbox")
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()
    except Exception as exc:
        print(f"[browser_test] Error clicking checkbox: {exc}")
        return None

    print(f"[browser_test] Waiting up to {timeout_s}s for token generation...")
    print("[browser_test] (If an interactive image challenge appears, solve it in the browser window)")

    prompt_detected = False
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        # Check if challenge prompt is visible
        if not prompt_detected:
            for f in page.frames:
                try:
                    if await f.locator(".prompt-text").count() > 0:
                        prompt_text = await f.locator(".prompt-text").first.text_content()
                        if prompt_text and prompt_text.strip():
                            print(f"[browser_test] >>> Interactive challenge prompted: '{prompt_text.strip()}' <<<")
                            prompt_detected = True
                            break
                except Exception:
                    pass

        # Check for token in main page
        try:
            token = await page.evaluate(
                "() => document.querySelector('[name=\"h-captcha-response\"]')?.value || "
                "document.querySelector('[name=\"g-recaptcha-response\"]')?.value || ''"
            )
            if token and len(token) > 10:
                print(f"[browser_test] ✓ Token successfully captured ({len(token)} chars)!")
                return token
        except Exception:
            pass

        await asyncio.sleep(0.5)

    print("[browser_test] Timeout reached waiting for token.")
    return None


async def run_browser_test(
    url: Optional[str] = None,
    headless: Optional[bool] = None,
    timeout_s: int = 60,
) -> dict:
    from captcha_service import verify_token

    target_url = url or DEFAULT_DEMO_URL
    is_headless = config.BROWSER_HEADLESS if headless is None else headless

    session = BrowserSession(headless=is_headless)
    result = {
        "test": "browser_token_extraction",
        "url": target_url,
        "headless": is_headless,
        "token_extracted": False,
        "token_preview": None,
        "token_length": 0,
        "server_verify": None,
        "error": None,
    }

    try:
        await session.start()
        page = await session.new_page()
        token = await extract_token_from_page(page, url=target_url, timeout_s=timeout_s)

        if token:
            result["token_extracted"] = True
            result["token_preview"] = token[:32] + "..."
            result["token_length"] = len(token)

            # Attempt verification with local secret key
            vr = verify_token(token)
            result["server_verify"] = {"success": vr.success, "raw": vr.raw, "error": vr.error}
        else:
            result["error"] = "token_extraction_failed"

        # If running with a visible browser, pause briefly so user sees the result
        if not is_headless:
            print("[browser_test] Keeping browser open for 5 seconds to inspect final state...")
            await asyncio.sleep(5)

    except Exception as exc:
        result["error"] = f"browser_error:{type(exc).__name__}:{exc}"
    finally:
        await session.stop()

    return result


def main():
    parser = argparse.ArgumentParser(description="hCaptcha Playwright automation test")
    parser.add_argument("--url", default=DEFAULT_DEMO_URL, help="Target URL with hCaptcha widget")
    parser.add_argument(
        "--headless",
        type=lambda v: str(v).lower() in ("true", "1", "yes"),
        default=config.BROWSER_HEADLESS,
        help="Run browser in headless mode (true/false)",
    )
    parser.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")

    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f" Target URL : {args.url}")
    print(f" Headless   : {args.headless}")
    print(f" Timeout    : {args.timeout}s")
    print(f"{'='*60}\n")

    res = asyncio.run(run_browser_test(url=args.url, headless=args.headless, timeout_s=args.timeout))

    print(f"\n{'-'*60}")
    print(f" Extracted : {res['token_extracted']}")
    if res["token_extracted"]:
        print(f" Preview   : {res['token_preview']} ({res['token_length']} chars)")
        print(f" Verify    : {res['server_verify']}")
    else:
        print(f" Error     : {res['error']}")
    print(f"{'-'*60}\n")


if __name__ == "__main__":
    main()
