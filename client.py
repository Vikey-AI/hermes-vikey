"""Talking to Vikey: sign-in (device login), the module list, and the decision calls.

Shared with the OpenCode plugin on the same computer:
  ~/.vikey/auth.json     the device token from "Login dengan Vikey" (mode 600)
  ~/.vikey/config.json   optional {"url": "..."} to point at another server (dev)
Environment overrides: VIKEY_PLUGINS_URL, VIKEY_PLUGINS_TOKEN, or VIKEY_API_KEY (a regular
Vikey API key, for servers without a browser).
"""

from __future__ import annotations

import json
import os
import platform
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_URL = "https://plugins.vikey.ai"
HOME = Path(os.environ.get("VIKEY_HOME") or (Path.home() / ".vikey")).expanduser()
AUTH_FILE = HOME / "auth.json"
CONFIG_FILE = HOME / "config.json"
VERSION = "0.1.0"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def base_url() -> str:
    return (os.environ.get("VIKEY_PLUGINS_URL") or _read_json(CONFIG_FILE).get("url") or DEFAULT_URL).rstrip("/")


def token() -> str:
    return os.environ.get("VIKEY_PLUGINS_TOKEN") or _read_json(AUTH_FILE).get("token") or os.environ.get("VIKEY_API_KEY") or ""


def save_token(value: str, user: Optional[Dict[str, Any]] = None) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    data = {"token": value, "user": user or {}, "savedAt": int(time.time())}
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def forget_token() -> None:
    try:
        AUTH_FILE.unlink()
    except FileNotFoundError:
        pass


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def request(method: str, path: str, body: Optional[Dict[str, Any]] = None, *, timeout: float = 6.0, auth: bool = True) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": f"vikey-hermes/{VERSION}", "X-Vikey-Agent": "hermes"}
    if auth:
        t = token()
        if not t:
            raise ApiError(401, "login_required", "Belum login ke Vikey. Jalankan: hermes vikey login")
        headers["Authorization"] = f"Bearer {t}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        err = payload.get("error")
        if isinstance(err, dict):
            raise ApiError(e.code, str(err.get("code") or ""), str(err.get("message") or f"HTTP {e.code}"))
        raise ApiError(e.code, str(err or ""), str(payload.get("error_description") or err or f"HTTP {e.code}"))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ApiError(0, "unreachable", f"Server Vikey tidak terjangkau ({e})")


def device_name() -> str:
    host = socket.gethostname().split(".")[0] or "komputer"
    return f"Hermes di {host} ({platform.system()})"[:80]


def login(print_fn=print, open_browser: bool = True) -> Tuple[bool, str]:
    """Device login: show a code and a link, wait until the user approves it in the browser."""
    start = request("POST", "/v1/device/code", {"client": "hermes", "device_name": device_name()}, auth=False)
    link = start["verification_uri_complete"]
    print_fn("Login dengan Vikey")
    print_fn(f"  Buka: {link}")
    print_fn(f"  Kode: {start['user_code']}")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(link)
        except Exception:
            pass
    interval = max(int(start.get("interval") or 5), 2)
    deadline = time.time() + int(start.get("expires_in") or 600)
    while time.time() < deadline:
        time.sleep(interval)
        try:
            got = request("POST", "/v1/device/token", {"device_code": start["device_code"]}, auth=False)
        except ApiError as e:
            if e.code == "authorization_pending":
                continue
            if e.code == "slow_down":
                interval += 2
                continue
            if e.code == "access_denied":
                return False, "Login ditolak di browser."
            if e.code == "expired_token":
                return False, "Kode login kedaluwarsa. Ulangi login."
            if e.code == "unreachable":
                continue
            return False, str(e)
        save_token(got["access_token"])
        try:
            me = request("GET", "/v1/me")
            save_token(got["access_token"], me.get("user"))
            return True, f"Berhasil login sebagai {me.get('user', {}).get('name') or me.get('user', {}).get('email') or 'akun Vikey'}."
        except ApiError:
            return True, "Berhasil login."
    return False, "Waktu login habis. Ulangi login."


class Modules:
    """Which modules are active for this account, refreshed in the background every few minutes."""

    TTL = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._at = 0.0
        self._refreshing = False
        self._loaded = threading.Event()  # set once the first refresh finished (ok or not)
        self.store_url = ""

    def refresh(self, wait: bool = False) -> None:
        def run():
            try:
                me = request("GET", "/v1/me", timeout=5)
                with self._lock:
                    self._state = me.get("modules") or {}
                    self.store_url = me.get("store") or ""
                    self._at = time.time()
            except ApiError as e:
                with self._lock:
                    if e.status in (401, 403):
                        self._state = {}
                    self._at = time.time() - self.TTL + 30  # retry soon
            finally:
                self._refreshing = False
                self._loaded.set()

        if self._refreshing:
            return
        self._refreshing = True
        if wait:
            run()
        else:
            threading.Thread(target=run, name="vikey-modules", daemon=True).start()

    def active(self, key: str) -> bool:
        if not self._loaded.is_set():
            # First message of a session: wait briefly for the module list instead of skipping.
            self.refresh()
            self._loaded.wait(timeout=4)
        elif time.time() - self._at > self.TTL:
            self.refresh()
        with self._lock:
            return bool(self._state.get(key, {}).get("active"))

    def message(self, key: str) -> str:
        with self._lock:
            return str(self._state.get(key, {}).get("message") or "")

    def mark_inactive(self, key: str, message: str = "") -> None:
        with self._lock:
            self._state[key] = {"active": False, "message": message}
            self._at = time.time()


modules = Modules()
