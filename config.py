# language: Python, file: config.py
# Loads and validates all environment config. Single source of truth.

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SITE_KEY: str = os.getenv("HCAPTCHA_SITE_KEY", "10000000-ffff-ffff-ffff-000000000001")
    SECRET_KEY: str = os.getenv("HCAPTCHA_SECRET_KEY", "0x0000000000000000000000000000000000000000")
    VERIFY_URL: str = os.getenv("HCAPTCHA_VERIFY_URL", "https://api.hcaptcha.com/siteverify")
    TEST_RESPONSE: str = os.getenv("HCAPTCHA_TEST_RESPONSE", "10000000-aaaa-bbbb-cccc-000000000001")
    BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
    DEMO_URL: str = os.getenv("HCAPTCHA_DEMO_URL", "https://nopecha.com/demo/hcaptcha")
    GEMINI_API_KEY_FALLBACK: str = os.getenv("GEMINI_API_KEY_FALLBACK", "")
    _raw_gemini: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_API_KEY: str = _raw_gemini.split(",")[0].strip() if _raw_gemini else ""

    @classmethod
    def get_gemini_keys(cls) -> list[str]:
        keys: list[str] = []
        raw_candidates = [
            os.getenv("GEMINI_API_KEYS", ""),
            os.getenv("GEMINI_API_KEY", ""),
            cls.GEMINI_API_KEY_FALLBACK,
            os.getenv("GEMINI_FALLBACK_KEY", ""),
        ]
        for candidate in raw_candidates:
            if not candidate:
                continue
            for k in candidate.split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys

    @classmethod
    def validate(cls) -> None:
        missing = [
            field for field in ("SITE_KEY", "SECRET_KEY", "VERIFY_URL")
            if not getattr(cls, field)
        ]
        if missing:
            raise EnvironmentError(f"Missing required config fields: {missing}")


# Populate keys list on class for direct access
Config.GEMINI_API_KEYS = Config.get_gemini_keys()
config = Config()

