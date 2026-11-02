# language: Python, file: main.py
# Entry point. Run: python main.py

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from config import config, Config
from captcha_service import (
    verify_token,
    verify_with_wrong_secret,
    verify_missing_token,
    verify_invalid_token,
)
from browser_test import run_browser_test

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)


def _record(name: str, fn, *args, **kwargs) -> dict:
    start = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        elapsed = round(time.perf_counter() - start, 4)
        if hasattr(result, "success"):
            return {
                "test": name,
                "passed": result.success,
                "elapsed_s": elapsed,
                "raw": result.raw,
                "local_error": result.error,
            }
        return {"test": name, "passed": True, "elapsed_s": elapsed, "raw": result}
    except Exception as exc:
        elapsed = round(time.perf_counter() - start, 4)
        return {"test": name, "passed": False, "elapsed_s": elapsed, "raw": {}, "local_error": f"{type(exc).__name__}: {exc}"}


def run_server_tests() -> list:
    return [
        _record("valid_test_token",   verify_token, config.TEST_RESPONSE),
        _record("invalid_token",      verify_invalid_token),
        _record("missing_token",      verify_missing_token),
        _record("wrong_secret",       verify_with_wrong_secret, config.TEST_RESPONSE),
        _record("reuse_first",        verify_token, config.TEST_RESPONSE),
        _record("reuse_second",       verify_token, config.TEST_RESPONSE),
    ]


async def run_all() -> dict:
    Config.validate()

    suite_start = time.perf_counter()
    print("[hcaptcha-tool] running server-side tests...")
    server_results = run_server_tests()

    print("[hcaptcha-tool] running browser test...")
    browser_result = await run_browser_test()

    total_elapsed = round(time.perf_counter() - suite_start, 4)
    all_results = server_results + [browser_result]

    passed = sum(1 for r in all_results if r.get("passed") or r.get("token_extracted"))
    failed = len(all_results) - passed

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_tests": len(all_results),
        "passed": passed,
        "failed": failed,
        "total_elapsed_s": total_elapsed,
        "results": all_results,
    }

    report_path = REPORTS_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(f"\n{'─'*50}")
    print(f"  Tests run : {len(all_results)}")
    print(f"  Passed    : {passed}")
    print(f"  Failed    : {failed}")
    print(f"  Time      : {total_elapsed}s")
    print(f"  Report    : {report_path}")
    print(f"{'─'*50}\n")

    return report


if __name__ == "__main__":
    asyncio.run(run_all())
