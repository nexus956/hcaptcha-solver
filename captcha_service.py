# language: Python, file: captcha_service.py
# All server-side hCaptcha verification logic. Stateless functions, error-isolated.

import requests
from typing import Optional
from config import config


class VerifyResult:
    def __init__(self, success: bool, raw: dict, error: Optional[str] = None):
        self.success = success
        self.raw = raw
        self.error = error

    def __repr__(self) -> str:
        return f"VerifyResult(success={self.success}, error={self.error}, raw={self.raw})"


def verify_token(
    token: str,
    secret: Optional[str] = None,
    site_key: Optional[str] = None,
    timeout: Optional[int] = None,
) -> VerifyResult:
    secret = secret or config.SECRET_KEY
    site_key = site_key or config.SITE_KEY
    timeout = timeout or config.REQUEST_TIMEOUT

    if not token or not token.strip():
        return VerifyResult(success=False, raw={}, error="empty_token")

    payload = {"secret": secret, "response": token, "sitekey": site_key}

    try:
        resp = requests.post(config.VERIFY_URL, data=payload, timeout=timeout)
        resp.raise_for_status()
        data: dict = resp.json()
        return VerifyResult(success=bool(data.get("success")), raw=data)
    except requests.exceptions.Timeout:
        return VerifyResult(success=False, raw={}, error="timeout")
    except requests.exceptions.ConnectionError:
        return VerifyResult(success=False, raw={}, error="connection_error")
    except requests.exceptions.HTTPError as exc:
        return VerifyResult(success=False, raw={}, error=f"http_error:{exc.response.status_code}")
    except ValueError:
        return VerifyResult(success=False, raw={}, error="invalid_json")
    except Exception as exc:
        return VerifyResult(success=False, raw={}, error=f"unexpected:{type(exc).__name__}")


def verify_with_wrong_secret(token: str) -> VerifyResult:
    return verify_token(token, secret="0x0000000000000000000000000000000000000001")


def verify_missing_token() -> VerifyResult:
    return verify_token("")


def verify_invalid_token() -> VerifyResult:
    return verify_token("invalid-token-xxxx-yyyy-zzzz")
