"""oauth client-credentials session for the datafordeler fildownload api."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.datafordeler.dk"
DEFAULT_TOKEN_URL = "https://auth.datafordeler.dk/realms/distribution/protocol/openid-connect/token"


class Session:
    """oauth client-credentials holder: bearer rides the header, refreshed on a mid-job 401."""

    def __init__(self, base: str, token_url: str, client_id: str, client_secret: str):
        self.base = base.rstrip("/")
        self._token_url = token_url
        self._cid = client_id
        self._secret = client_secret
        self._token: str | None = None

    def _bearer(self) -> str:
        if self._token is None:
            cred = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(
                self._token_url,
                data=body,
                headers={
                    "Authorization": f"Basic {cred}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            token = data.get("access_token")
            if not token:  # 200 with an error body -> no point retrying bad creds
                raise SystemExit(
                    f"[!] token endpoint: no access_token ({data.get('error') or 'unknown'})"
                )
            self._token = token
        return self._token

    def open(self, path: str, *, range_start: int | None = None, timeout: int = 300):
        # GET base+path; one retry after a token refresh on 401 (token is valid ~60m)
        url = f"{self.base}{path}"
        for refreshed in (False, True):
            headers = {"Authorization": f"Bearer {self._bearer()}"}
            if range_start:
                headers["Range"] = f"bytes={range_start}-"
            req = urllib.request.Request(url, headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed:
                    self._token = None
                    continue
                raise
        raise AssertionError("unreachable")
