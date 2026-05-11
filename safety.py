#!/usr/bin/env python3
"""
safety.py — Shared safety module for the Bybit arbitrage bot system.

Provides:
  - openssl_sign()          — RSA-SHA256 signing via openssl with retry
  - SafeBybitAPI            — Rate-limited, retry-capable API wrapper
  - make_safe_get / make_safe_post — Compatibility wrappers for existing code
  - atomic_write()          — Atomic JSON file writes via write-to-tmp + rename
  - atomic_read()           — Atomic JSON file reads (real file, then .tmp fallback)
  - validate_startup()      — Pre-flight checks (key, file, API, balance)
  - SignError               — Custom exception for signing failures

Import by all bot scripts (bybit_execution.py, health_check.py, funding_v3.py).
All imports are standard library only (os, json, time, subprocess, urllib).
"""

import os
import json
import time
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import hashlib


# =============================================================================
# Custom Exceptions
# =============================================================================

class SignError(Exception):
    """Raised when openssl signing fails after all retries."""
    pass


# =============================================================================
# Signing
# =============================================================================

def openssl_sign(param_str: str, key_path: str, max_retries: int = 2) -> str:
    """
    Sign a string using openssl RSA-SHA256.

    Calls ``openssl dgst -sha256 -sign <key_path> -binary``, pipes the result
    through ``base64 -w0``, and returns the base64-encoded signature string.

    Retries *max_retries* times on failure (openssl can hang on bad keys).

    Args:
        param_str: The string to sign (e.g. timestamp + api_key + recv_window + query).
        key_path:  Absolute path to the RSA private key file (PEM format).
        max_retries: Number of additional attempts on failure (default 2, total = 3).

    Returns:
        Base64-encoded signature string.

    Raises:
        SignError: If all retries are exhausted.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", key_path, "-binary"],
                input=param_str.encode(),
                capture_output=True,
                timeout=5,
            )
            if proc.returncode != 0:
                stderr = proc.stderr.decode(errors="replace").strip()
                raise SignError(
                    f"openssl exited with code {proc.returncode}: {stderr}"
                )

            sig = subprocess.run(
                ["base64", "-w0"],
                input=proc.stdout,
                capture_output=True,
                timeout=5,
            ).stdout.decode().strip()

            if not sig:
                raise SignError("openssl produced empty signature")

            return sig

        except subprocess.TimeoutExpired as e:
            last_exc = SignError(f"openssl timed out (attempt {attempt + 1}): {e}")
        except subprocess.CalledProcessError as e:
            last_exc = SignError(f"subprocess error (attempt {attempt + 1}): {e}")
        except FileNotFoundError as e:
            raise SignError(f"openssl or base64 binary not found: {e}") from e
        except OSError as e:
            last_exc = SignError(f"OS error reading key file (attempt {attempt + 1}): {e}")
        except SignError:
            raise  # Re-raise our own exception immediately

        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))  # Brief back-off before retry

    raise SignError(f"openssl signing failed after {1 + max_retries} attempts") from last_exc


# =============================================================================
# Rate-limited, retry-capable Bybit API wrapper
# =============================================================================

class SafeBybitAPI:
    """
    Thread-safe (by virtue of single-threaded usage) wrapper for Bybit V5 REST API.

    Features:
      - RSA-SHA256 signing via ``openssl_sign()``
      - Rate limiting: max 10 calls/second (sliding window)
      - Retry with exponential backoff (1s, 2s, 4s) on:
          * HTTP/connection errors (timeout, connection reset, etc.)
          * Bybit rate limit (retCode 10006)
          * Bybit server errors (retCode >= 10000)
      - No retry on:
          * Authentication errors (retCode 10001-10004 — wrong key)
          * Bad request / validation errors (retCode 170001-179999)

    Usage::

        api = SafeBybitAPI(
            base_url="https://api.bybit.com",
            api_key="...",
            key_path="/path/to/private.pem",
        )
        result = api.get("/v5/market/tickers", {"category": "spot"})
        result = api.post("/v5/order/create", {"symbol": "BTCUSDT", ...})
    """

    # ── Constants ──────────────────────────────────────────────────────
    MAX_CALLS_PER_SECOND = 10
    WINDOW_SECONDS = 1.0

    RETRY_ATTEMPTS = 3              # Total attempts (1 initial + 2 retries)
    RETRY_BACKOFFS = [1.0, 2.0, 4.0]

    BYBIT_RATE_LIMIT_CODE = 10006
    # retCode ranges that should NOT be retried
    NO_RETRY_RANGES = [
        (10001, 10004),   # Authentication errors (wrong key, permissions)
        (170001, 179999), # Bad request / validation errors
    ]

    # ── Constructor ────────────────────────────────────────────────────

    def __init__(self, base_url: str, api_key: str, key_path: str):
        """
        Args:
            base_url: Bybit API base URL (e.g. ``https://api.bybit.com``).
            api_key:  Bybit API key string.
            key_path: Absolute path to the RSA private key file.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.key_path = key_path
        self.user_agent = "safety-module/1.0"

        # Rate-limit tracking
        self._call_timestamps: list[float] = []

    # ── Rate-Limit Helpers ─────────────────────────────────────────────

    def _wait_for_rate_limit(self):
        """
        Block until we're allowed to make another API call.

        Uses a simple sliding-window counter: if ``MAX_CALLS_PER_SECOND``
        calls have been made in the last ``WINDOW_SECONDS`` seconds, sleep
        until the oldest call falls out of the window.
        """
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS

        # Prune timestamps older than the window
        self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]

        if len(self._call_timestamps) >= self.MAX_CALLS_PER_SECOND:
            # Sleep until the oldest timestamp falls out of the window
            sleep_for = self._call_timestamps[0] + self.WINDOW_SECONDS - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            # Re-prune after sleeping
            now = time.monotonic()
            cutoff = now - self.WINDOW_SECONDS
            self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]

        self._call_timestamps.append(time.monotonic())

    def get_rate_limit_status(self) -> dict:
        """
        Return approximate rate-limit status.

        Returns:
            dict with keys:
              - ``calls_remaining``: estimated calls available now
              - ``reset_time``: monotonic timestamp when the window resets
        """
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        recent = [t for t in self._call_timestamps if t > cutoff]
        calls_made = len(recent)
        calls_remaining = max(0, self.MAX_CALLS_PER_SECOND - calls_made)

        if recent:
            reset_time = recent[0] + self.WINDOW_SECONDS
        else:
            reset_time = now

        return {
            "calls_remaining": calls_remaining,
            "reset_time": reset_time,
        }

    # ── Signing ────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str, query: str = "", body_str: str = "") -> dict:
        """
        Build signed headers for a Bybit V5 API request.

        Args:
            method:   ``"GET"`` or ``"POST"``.
            path:     API path (e.g. ``/v5/market/tickers``).
            query:    URL-encoded query string (GET only).
            body_str: JSON-encoded body string (POST only).

        Returns:
            dict of HTTP headers including ``X-BAPI-*`` headers.
        """
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        param_str = f"{timestamp}{self.api_key}{recv_window}"
        if method == "GET":
            param_str += query
        else:
            param_str += body_str

        signature = openssl_sign(param_str, self.key_path)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN-TYPE": "2",
            "User-Agent": self.user_agent,
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"

        return headers

    # ── Core request method with retry ─────────────────────────────────

    def _request(self, method: str, path: str, body: dict = None,
                 params: dict = None) -> dict:
        """
        Execute an authenticated HTTP request with rate limiting and retry.

        Args:
            method: ``"GET"`` or ``"POST"``.
            path:   API path (e.g. ``/v5/market/tickers``).
            body:   JSON-serialisable dict (POST only, optional).
            params: URL query parameters (GET only, optional).

        Returns:
            Parsed JSON response dict from the Bybit API.
        """
        query = urllib.parse.urlencode(params) if params else ""
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        last_error = None

        for attempt in range(self.RETRY_ATTEMPTS):
            # ── Rate limit gate ────────────────────────────────────
            self._wait_for_rate_limit()

            # ── Build request ──────────────────────────────────────
            try:
                headers = self._sign(method, path, query=query, body_str=body_str)
            except SignError as e:
                # Signing failures are fatal — no point retrying
                return {"retCode": -1, "retMsg": f"Signing failed: {e}"}

            url = f"{self.base_url}{path}"
            if method == "GET" and query:
                url = f"{url}?{query}"

            req = urllib.request.Request(
                url,
                data=body_str.encode() if method == "POST" else None,
                headers=headers,
                method=method,
            )

            # ── Execute ───────────────────────────────────────────
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # Try to parse error body for Bybit retCode
                try:
                    err_body = json.loads(e.read())
                except Exception:
                    err_body = {"retCode": -1, "retMsg": str(e)}
                result = err_body
            except (urllib.error.URLError, OSError, ValueError) as e:
                # Network error, timeout, connection reset, JSON parse error
                result = {"retCode": -1, "retMsg": str(e)}

            # ── Decide whether to retry ───────────────────────────
            ret_code = result.get("retCode", -1)

            if ret_code == 0:
                # Success — return immediately
                return result

            # Check if this is a non-retryable error
            if self._is_non_retryable(ret_code):
                return result

            # Retryable error — log and back off
            last_error = result
            ret_msg = result.get("retMsg", "unknown error")
            if attempt < self.RETRY_ATTEMPTS - 1:
                backoff = self.RETRY_BACKOFFS[attempt]
                time.sleep(backoff)

        # All retries exhausted
        return {
            "retCode": last_error.get("retCode", -1) if last_error else -1,
            "retMsg": f"All {self.RETRY_ATTEMPTS} attempts failed. "
                      f"Last error: {last_error.get('retMsg', 'unknown') if last_error else 'unknown'}",
        }

    @staticmethod
    def _is_non_retryable(ret_code: int) -> bool:
        """Return ``True`` if the retCode should NOT be retried."""
        if ret_code == 0:
            return False
        for lo, hi in SafeBybitAPI.NO_RETRY_RANGES:
            if lo <= ret_code <= hi:
                return True
        return False

    # ── Public Methods ─────────────────────────────────────────────────

    def get(self, path: str, params: dict = None) -> dict:
        """
        Authenticated GET request to Bybit with rate limiting and retry.

        Args:
            path:   API path (e.g. ``/v5/market/tickers``).
            params: Optional query parameters dict.

        Returns:
            Parsed JSON response dict.
        """
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        """
        Authenticated POST request to Bybit with rate limiting and retry.

        Args:
            path: API path (e.g. ``/v5/order/create``).
            body: JSON-serialisable request body dict.

        Returns:
            Parsed JSON response dict.
        """
        return self._request("POST", path, body=body)


# =============================================================================
# Compatibility Wrappers
# =============================================================================

def make_safe_get(api: SafeBybitAPI):
    """
    Return a bound ``get`` method so existing code can do::

        bybit_get = make_safe_get(api)
        result = bybit_get("/v5/market/tickers", {"category": "spot"})

    This matches the signature of the original ``bybit_get(path, params=None)``
    functions used in ``bybit_execution.py``, ``health_check.py``, and
    ``funding_v3.py``.
    """
    return api.get


def make_safe_post(api: SafeBybitAPI):
    """
    Return a bound ``post`` method so existing code can do::

        bybit_post = make_safe_post(api)
        result = bybit_post("/v5/order/create", {"symbol": "BTCUSDT", ...})

    This matches the signature of the original ``bybit_post(path, body)``
    functions used in the bot scripts.
    """
    return api.post


# =============================================================================
# Atomic File Operations
# =============================================================================

def atomic_write(path: str, data: any):
    """
    Atomically write JSON-serialisable *data* to *path*.

    Strategy:
      1. Serialise *data* as pretty-printed JSON.
      2. Write to ``path + ".tmp"``.
      3. ``os.rename(path + ".tmp", path)`` — atomic on Linux (same filesystem).

    If the write to ``.tmp`` fails, the ``.tmp`` file is left behind for
    potential clean-up. If the rename fails, the ``.tmp`` file survives.

    Args:
        path: Destination file path.
        data: Any JSON-serialisable Python object.

    Raises:
        OSError: If the write or rename fails at the OS level.
        TypeError: If *data* is not JSON-serialisable.
    """
    tmp_path = path + ".tmp"
    tmp_path_atomic = tmp_path  # Create once for readability

    # Write to temporary file
    with open(tmp_path_atomic, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())  # Ensure data hits disk

    # Atomic rename (same filesystem)
    os.rename(tmp_path_atomic, path)


def atomic_read(path: str) -> any:
    """
    Atomically read JSON data from *path*.

    1. Try to read and parse ``path`` (the real file).
    2. If that fails, try ``path + ".tmp"`` (leftover from a crashed atomic_write).
    3. Return ``None`` if both fail.

    Args:
        path: File path to read.

    Returns:
        Parsed JSON data, or ``None`` if neither file exists or is valid JSON.
    """
    for candidate in (path, path + ".tmp"):
        try:
            with open(candidate) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return None


# =============================================================================
# Startup Validation
# =============================================================================

def validate_startup(api_key: str, key_path: str, min_balance: float = 0) -> dict:
    """
    Perform pre-flight checks before running the bot.

    Checks performed:
      1. API key is not empty.
      2. Private key file exists and is readable.
      3. Can make a successful API call (wallet balance query).
      4. Wallet balance >= ``min_balance`` (only if ``min_balance > 0``).

    Args:
        api_key:     The Bybit API key string.
        key_path:    Absolute path to the RSA private key file.
        min_balance: Minimum required wallet balance in USDT (0 = skip check).

    Returns:
        dict with structure::

            {
                "ok": True | False,
                "checks": {
                    "api_key": True | False,
                    "key_file": True | False,
                    "api_call": True | False,
                    "balance": True | False,
                },
                "wallet": float | None,
                "errors": [str, ...],
            }
    """
    result: dict = {
        "ok": True,
        "checks": {
            "api_key": False,
            "key_file": False,
            "api_call": False,
            "balance": False,
        },
        "wallet": None,
        "errors": [],
    }

    # ── 1. API key ─────────────────────────────────────────────────────
    if not api_key or not api_key.strip():
        result["checks"]["api_key"] = False
        result["errors"].append("API key is empty or missing")
        result["ok"] = False
    else:
        result["checks"]["api_key"] = True

    # ── 2. Private key file ────────────────────────────────────────────
    if not key_path:
        result["checks"]["key_file"] = False
        result["errors"].append("Private key path is empty")
        result["ok"] = False
    elif not os.path.exists(key_path):
        result["checks"]["key_file"] = False
        result["errors"].append(f"Private key file not found: {key_path}")
        result["ok"] = False
    elif not os.access(key_path, os.R_OK):
        result["checks"]["key_file"] = False
        result["errors"].append(f"Private key file not readable: {key_path}")
        result["ok"] = False
    else:
        result["checks"]["key_file"] = True

    # ── 3. API call test (wallet balance) ──────────────────────────────
    if result["checks"]["api_key"] and result["checks"]["key_file"]:
        try:
            # Build a minimal SafeBybitAPI instance to avoid circular imports
            api = SafeBybitAPI(
                base_url="https://api.bybit.com",
                api_key=api_key,
                key_path=key_path,
            )
            resp = api.get("/v5/account/wallet-balance", {
                "accountType": "UNIFIED",
                "coin": "USDT",
            })

            if resp.get("retCode") == 0:
                result["checks"]["api_call"] = True
                # Extract wallet balance
                coin_list = (
                    resp.get("result", {})
                    .get("list", [{}])[0]
                    .get("coin", [])
                )
                wallet_balance = 0.0
                for coin_entry in coin_list:
                    if coin_entry.get("coin") == "USDT":
                        wallet_balance = float(coin_entry.get("walletBalance", 0))
                        break
                result["wallet"] = wallet_balance
            else:
                ret_msg = resp.get("retMsg", "unknown error")
                result["checks"]["api_call"] = False
                result["errors"].append(
                    f"API call failed (retCode={resp.get('retCode')}): {ret_msg}"
                )
                result["ok"] = False
        except Exception as e:
            result["checks"]["api_call"] = False
            result["errors"].append(f"API call exception: {e}")
            result["ok"] = False
    else:
        result["checks"]["api_call"] = False

    # ── 4. Balance check ───────────────────────────────────────────────
    if min_balance > 0 and result["wallet"] is not None:
        if result["wallet"] >= min_balance:
            result["checks"]["balance"] = True
        else:
            result["checks"]["balance"] = False
            result["errors"].append(
                f"Wallet balance ${result['wallet']:.2f} is below "
                f"minimum ${min_balance:.2f}"
            )
            result["ok"] = False
    elif min_balance > 0:
        result["checks"]["balance"] = False
        result["errors"].append(
            f"Cannot verify balance (wallet data unavailable)"
        )
        result["ok"] = False
    else:
        # Balance check skipped (min_balance == 0)
        result["checks"]["balance"] = True

    return result
