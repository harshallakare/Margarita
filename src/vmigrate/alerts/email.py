"""Purpose: the email (SMTP) notification channel.

Config (notification_channels.config; the password lives in the secret store, never in config):
    host: smtp.example.com      port: 587          security: starttls | ssl | none
    username: alerts@example.com (optional)        from_addr: margarita@example.com
    to: [ops@example.com, ...]   verify_tls: true  subject_prefix: "[Margarita]"

Usage:
    ch = EmailChannel(config, password)
    ch.check()                                   # connect + TLS + login, sends nothing (save-time check)
    ch.send("subject", "plain-text body")
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

from ..core.errors import ConfigError

SECURITY = ("starttls", "ssl", "none")


def validate(cfg: dict[str, Any]) -> list[str]:
    p: list[str] = []
    if not str(cfg.get("host") or "").strip():
        p.append("host is required")
    try:
        port = int(cfg.get("port") or 0)
        if not 0 < port < 65536:
            p.append("port must be 1-65535")
    except (TypeError, ValueError):
        p.append("port must be a number")
    if cfg.get("security", "starttls") not in SECURITY:
        p.append(f"security must be one of {', '.join(SECURITY)}")
    if "@" not in parseaddr(str(cfg.get("from_addr") or ""))[1]:
        p.append("from_addr must be an email address")
    to = cfg.get("to") or []
    if not to or any("@" not in parseaddr(str(a))[1] for a in to):
        p.append("to must list at least one email address")
    return p


class EmailChannel:
    def __init__(self, cfg: dict[str, Any], password: str | None = None, *, timeout: float = 20,
                 smtp_factory: Any = None):
        bad = validate(cfg)
        if bad:
            raise ConfigError("email channel: " + "; ".join(bad))
        self.cfg, self.password, self.timeout = cfg, password, timeout
        self.security = cfg.get("security", "starttls")
        self._smtp = smtp_factory or (smtplib.SMTP_SSL if self.security == "ssl" else smtplib.SMTP)

    def _tls(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self.cfg.get("verify_tls") is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self) -> Any:
        host, port = str(self.cfg["host"]).strip(), int(self.cfg["port"])
        if self.security == "ssl":
            s = self._smtp(host, port, timeout=self.timeout, context=self._tls())
        else:
            s = self._smtp(host, port, timeout=self.timeout)
        try:
            s.ehlo()
            if self.security == "starttls":
                s.starttls(context=self._tls())
                s.ehlo()
            if self.cfg.get("username"):
                s.login(str(self.cfg["username"]), self.password or "")
        except Exception:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
            raise
        return s

    def check(self) -> None:
        """Connect, negotiate TLS and log in; sends nothing."""
        s = self._connect()
        s.quit()

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        prefix = str(self.cfg.get("subject_prefix") or "[Margarita]").strip()
        msg["Subject"] = f"{prefix} {subject}" if prefix else subject
        msg["From"] = str(self.cfg["from_addr"])
        msg["To"] = ", ".join(str(a) for a in self.cfg["to"])
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        msg["Message-ID"] = make_msgid(domain=parseaddr(str(self.cfg["from_addr"]))[1].split("@")[-1] or None)
        msg.set_content(body)
        s = self._connect()
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
