"""Purpose: Microsoft 365 alert email through the Microsoft Graph API (no SMTP AUTH needed).

An Entra ID app registration with the Graph *application* permission Mail.Send (admin-consented)
gets a token with the OAuth2 client-credentials grant and sends as the From mailbox:
    POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
    POST https://graph.microsoft.com/v1.0/users/{from_addr}/sendMail      -> 202 Accepted

Config (notification_channels.config; the client secret lives in the secret store):
    provider: office365_graph   tenant_id: <directory (tenant) id or domain>
    client_id: <application (client) id>   from_addr: alerts@contoso.com   to: [ops@contoso.com]
    subject_prefix: "[Margarita]"

Usage:
    ch = GraphMailChannel(config, client_secret)
    ch.check()          # gets a token: proves tenant, client id and secret (sends nothing)
    ch.send("subject", "plain-text body")

Not verified against a live tenant yet (the tests use a fake HTTP layer).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.utils import parseaddr
from typing import Any

from ..core.errors import ConfigError

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# (method, url, headers, body bytes | None, timeout) -> (status, body bytes)
Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def _urllib(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def validate(cfg: dict[str, Any]) -> list[str]:
    p: list[str] = []
    tenant = str(cfg.get("tenant_id") or "").strip()
    if not tenant or not (GUID.match(tenant) or "." in tenant):
        p.append("tenant_id must be the directory (tenant) ID or the tenant's domain")
    if not GUID.match(str(cfg.get("client_id") or "").strip()):
        p.append("client_id must be the application (client) ID, a GUID")
    if "@" not in parseaddr(str(cfg.get("from_addr") or ""))[1]:
        p.append("from_addr must be the sending mailbox's address")
    to = cfg.get("to") or []
    if not to or any("@" not in parseaddr(str(a))[1] for a in to):
        p.append("to must list at least one email address")
    return p


def _err(status: int, body: bytes) -> str:
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return f"HTTP {status}: {body[:300].decode(errors='replace')}"
    if isinstance(d.get("error"), dict):                     # Graph: {"error": {"code", "message"}}
        e = d["error"]
        return f"HTTP {status} {e.get('code')}: {e.get('message')}"
    if d.get("error_description"):                           # Entra token endpoint
        first = str(d["error_description"]).splitlines()[0]
        return f"HTTP {status} {d.get('error')}: {first}"
    return f"HTTP {status}: {str(d)[:300]}"


class GraphMailChannel:
    def __init__(self, cfg: dict[str, Any], client_secret: str | None, *, http: Http | None = None,
                 timeout: float = 20):
        bad = validate(cfg)
        if not client_secret:
            bad.append("client secret is required")
        if bad:
            raise ConfigError("Microsoft 365 channel: " + "; ".join(bad))
        self.cfg, self.secret, self.timeout = cfg, client_secret, timeout
        self.http = http or _urllib
        self._token: tuple[str, float] | None = None

    def _get_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        tenant = urllib.parse.quote(str(self.cfg["tenant_id"]).strip(), safe="")
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": str(self.cfg["client_id"]).strip(),
            "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"}).encode()
        st, raw = self.http("POST", f"{LOGIN}/{tenant}/oauth2/v2.0/token",
                            {"Content-Type": "application/x-www-form-urlencoded"}, body, self.timeout)
        if st != 200:
            raise PermissionError(f"Microsoft sign-in failed ({_err(st, raw)}). Check the tenant ID, "
                                  "client ID and client secret (secrets expire).")
        d = json.loads(raw)
        self._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return self._token[0]

    def check(self) -> None:
        """Token only: proves tenant, client ID and secret. Whether Mail.Send is granted for the From
        mailbox shows on the first send, so use Send test after saving."""
        self._get_token()

    def send(self, subject: str, body: str) -> None:
        prefix = str(self.cfg.get("subject_prefix") or "[Margarita]").strip()
        msg = {"message": {
            "subject": f"{prefix} {subject}" if prefix else subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": str(a)}} for a in self.cfg["to"]]},
            "saveToSentItems": False}
        sender = urllib.parse.quote(parseaddr(str(self.cfg["from_addr"]))[1], safe="@")
        st, raw = self.http("POST", f"{GRAPH}/users/{sender}/sendMail",
                            {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"},
                            json.dumps(msg).encode(), self.timeout)
        if st != 202:
            hint = (" The app needs the Microsoft Graph application permission Mail.Send with admin consent, "
                    "and an access policy (if any) that includes this mailbox." if st in (401, 403) else
                    " Check that the From address is an existing mailbox in this tenant." if st == 404 else "")
            raise RuntimeError(f"Microsoft Graph sendMail failed ({_err(st, raw)}).{hint}")
