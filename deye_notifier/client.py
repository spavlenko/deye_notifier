"""Thin client for the Deye Cloud OpenAPI (https://developer.deyecloud.com)."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Deye tokens are valid ~60 days; refresh a bit earlier.
TOKEN_TTL = 55 * 24 * 3600


class DeyeError(RuntimeError):
    pass


class DeyeClient:
    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        email: str,
        password: str,
        token_file: Path,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url
        self.app_id = app_id
        self.app_secret = app_secret
        self.email = email
        self.password = password
        self.token_file = token_file
        self.timeout = timeout
        self._token: str | None = None

    # ---- auth -------------------------------------------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        """Deye expects the SHA-256 of the password as lowercase hex."""
        if len(password) == 64 and all(c in "0123456789abcdef" for c in password):
            return password  # already hashed
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _load_cached_token(self) -> str | None:
        try:
            data = json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return None
        if data.get("expires_at", 0) > time.time():
            return data.get("access_token")
        return None

    def _fetch_token(self) -> str:
        log.info("Requesting new Deye Cloud access token")
        resp = self._post(
            f"/account/token?appId={self.app_id}",
            {
                "appSecret": self.app_secret,
                "email": self.email,
                "password": self.hash_password(self.password),
            },
            auth=False,
        )
        token = resp.get("accessToken")
        if not token:
            raise DeyeError(f"No accessToken in response: {resp}")
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(
            json.dumps({"access_token": token, "expires_at": time.time() + TOKEN_TTL})
        )
        self.token_file.chmod(0o600)
        return token

    def token(self, force: bool = False) -> str:
        if not force:
            self._token = self._token or self._load_cached_token()
        if force or not self._token:
            self._token = self._fetch_token()
        return self._token

    # ---- http -------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any], auth: bool = True) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth:
                raise TokenExpired() from e
            raise DeyeError(f"HTTP {e.code} on {path}: {e.read()[:300]!r}") from e
        except (OSError, http.client.HTTPException, ValueError) as e:  # URLError, timeouts, resets
            raise DeyeError(f"Request to {path} failed: {e}") from e

        if payload.get("success") is False:
            msg = payload.get("msg") or payload
            if auth and "token" in str(msg).lower():
                raise TokenExpired()
            raise DeyeError(f"{path}: {msg} (code {payload.get('code')})")
        return payload

    def call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST with automatic one-time token refresh."""
        try:
            return self._post(path, body)
        except TokenExpired:
            self.token(force=True)
            return self._post(path, body)

    # ---- endpoints --------------------------------------------------------

    def station_list(self) -> list[dict[str, Any]]:
        return self.call("/station/list", {"page": 1, "size": 50}).get("stationList", [])

    def station_with_devices(self) -> list[dict[str, Any]]:
        return self.call("/station/listWithDevice", {"page": 1, "size": 50}).get(
            "stationList", []
        )

    def station_latest(self, station_id: str) -> dict[str, Any]:
        return self.call("/station/latest", {"stationId": int(station_id)})

    def device_latest(self, device_sn: str) -> dict[str, Any] | None:
        resp = self.call("/device/latest", {"deviceList": [device_sn]})
        items = resp.get("deviceDataList") or []
        return items[0] if items else None

    def device_alerts(self, device_sn: str, start: int, end: int) -> list[dict[str, Any]]:
        """Alerts (faults/warnings) between two unix timestamps; max range 30 days."""
        alerts: list[dict[str, Any]] = []
        for page in range(1, 11):
            resp = self.call(
                "/device/alertList",
                {"deviceSn": device_sn, "startTimestamp": start, "endTimestamp": end,
                 "page": page, "size": 50},
            )
            batch = resp.get("alertList") or []
            alerts += batch
            if not batch or len(alerts) >= int(resp.get("total") or 0):
                break
        return alerts


class TokenExpired(DeyeError):
    pass
