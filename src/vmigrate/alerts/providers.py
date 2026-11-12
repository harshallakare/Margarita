"""Purpose: ready-made email providers for alert notifications. The operator picks a provider and
enters only the account and its secret; server, port and security come from the preset.

Two kinds:
  * smtp  — Gmail, Microsoft 365 (SMTP AUTH), Outlook.com, Yahoo, iCloud, Zoho, Fastmail, Amazon SES,
            SendGrid, Mailgun, Brevo, or any custom SMTP server (alerts.email.EmailChannel)
  * graph — Microsoft 365 through the Microsoft Graph API with an app registration (OAuth2 client
            credentials; alerts.graph.GraphMailChannel). Needs no SMTP AUTH on the mailbox.

Server names are each provider's published submission endpoints; they are not verified against a
live account by the tests (the provider fixtures are fakes).

Usage:
    cfg = apply_preset({"provider": "gmail", "username": "ops@gmail.com", ...})
    ch = make_channel(cfg, secret)       # EmailChannel or GraphMailChannel
    ch.check(); ch.send("subject", "body")
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ConfigError

PROVIDERS: dict[str, dict[str, Any]] = {
    "custom": {
        "label": "Custom SMTP server", "kind": "smtp", "host": "", "port": 587, "security": "starttls",
        "secret_label": "Password",
        "help": "Any SMTP server or internal relay. Leave the username empty for a relay that needs no login.",
    },
    "gmail": {
        "label": "Gmail / Google Workspace", "kind": "smtp", "host": "smtp.gmail.com", "port": 587,
        "security": "starttls", "username_is_from": True, "secret_label": "App password",
        "help": "Turn on 2-Step Verification for the account, then create an App password "
                "(Google Account → Security → App passwords). Your normal password is refused. "
                "The From address must be this account or one of its aliases.",
    },
    "office365_graph": {
        "label": "Microsoft 365 (Graph API, recommended)", "kind": "graph", "secret_label": "Client secret",
        "help": "In Microsoft Entra ID, register an app, add the Microsoft Graph application permission "
                "Mail.Send and grant admin consent, then create a client secret. The From address is the "
                "mailbox that sends (a user or shared mailbox). To limit the app to that one mailbox, add an "
                "Exchange application access policy or RBAC for Applications scope.",
    },
    "office365_smtp": {
        "label": "Microsoft 365 (SMTP AUTH)", "kind": "smtp", "host": "smtp.office365.com", "port": 587,
        "security": "starttls", "username_is_from": True, "secret_label": "Password",
        "help": "Authenticated SMTP must be enabled for the mailbox (Microsoft 365 admin center → user → Mail → "
                "Manage email apps → Authenticated SMTP).",
        "warning": "Microsoft is retiring basic-auth (username + password) SMTP AUTH for Exchange Online. "
                   "Prefer Microsoft 365 (Graph API), which uses an app registration instead.",
    },
    "outlook": {
        "label": "Outlook.com / Hotmail", "kind": "smtp", "host": "smtp-mail.outlook.com", "port": 587,
        "security": "starttls", "username_is_from": True, "secret_label": "Password or app password",
        "help": "With two-step verification on, create an app password (account.microsoft.com → Security).",
        "warning": "Microsoft has been moving Outlook.com away from password logins for SMTP; if the login "
                   "is refused, use a Microsoft 365 or other provider for alerts.",
    },
    "yahoo": {
        "label": "Yahoo Mail", "kind": "smtp", "host": "smtp.mail.yahoo.com", "port": 465, "security": "ssl",
        "username_is_from": True, "secret_label": "App password",
        "help": "Create an app password (Yahoo Account security → Generate app password).",
    },
    "icloud": {
        "label": "iCloud Mail", "kind": "smtp", "host": "smtp.mail.me.com", "port": 587, "security": "starttls",
        "username_is_from": True, "secret_label": "App-specific password",
        "help": "Create an app-specific password at account.apple.com → Sign-In and Security.",
    },
    "zoho": {
        "label": "Zoho Mail", "kind": "smtp", "host": "smtp.zoho.com", "port": 465, "security": "ssl",
        "username_is_from": True, "secret_label": "Password or app-specific password",
        "help": "Accounts in other data centres use smtp.zoho.eu, smtp.zoho.in or smtp.zoho.com.au "
                "(change the server under Server settings). With two-factor sign-in, use an app-specific password.",
    },
    "fastmail": {
        "label": "Fastmail", "kind": "smtp", "host": "smtp.fastmail.com", "port": 465, "security": "ssl",
        "username_is_from": True, "secret_label": "App password",
        "help": "Create an app password with SMTP access (Settings → Privacy & Security → App passwords).",
    },
    "amazon_ses": {
        "label": "Amazon SES", "kind": "smtp", "host": "email-smtp.us-east-1.amazonaws.com", "port": 587,
        "security": "starttls", "secret_label": "SMTP password",
        "help": "Use SES SMTP credentials (SES console → SMTP settings → Create SMTP credentials), not your AWS "
                "access keys. Change the region in the server name to your SES region. The From address must be "
                "a verified identity.",
    },
    "sendgrid": {
        "label": "SendGrid", "kind": "smtp", "host": "smtp.sendgrid.net", "port": 587, "security": "starttls",
        "username": "apikey", "secret_label": "API key",
        "help": "The username is literally \"apikey\"; the password is a SendGrid API key with Mail Send "
                "permission. The From address must be a verified sender.",
    },
    "mailgun": {
        "label": "Mailgun", "kind": "smtp", "host": "smtp.mailgun.org", "port": 587, "security": "starttls",
        "secret_label": "SMTP password",
        "help": "Use the domain's SMTP login (e.g. postmaster@mg.example.com) and its password. EU domains use "
                "smtp.eu.mailgun.org.",
    },
    "brevo": {
        "label": "Brevo (Sendinblue)", "kind": "smtp", "host": "smtp-relay.brevo.com", "port": 587,
        "security": "starttls", "secret_label": "SMTP key",
        "help": "Use the SMTP login and an SMTP key from Brevo → SMTP & API. The From address must be a "
                "verified sender.",
    },
}


def provider(cfg: dict[str, Any]) -> dict[str, Any]:
    return PROVIDERS.get(str(cfg.get("provider") or "custom"), PROVIDERS["custom"])


def apply_preset(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill server, port, security and (where the provider fixes it) the username from the preset.
    Values the operator set explicitly win, so a Zoho EU or SES region server can be entered."""
    p = provider(cfg)
    out = dict(cfg)
    if p["kind"] != "smtp":
        return out
    if not str(out.get("host") or "").strip():
        out["host"], out["port"], out["security"] = p["host"], p["port"], p["security"]
    if not out.get("username"):
        if p.get("username"):
            out["username"] = p["username"]
        elif p.get("username_is_from") and out.get("from_addr"):
            out["username"] = out["from_addr"]
    return out


def problems(cfg: dict[str, Any], secret: str | None) -> list[str]:
    """What is wrong with these settings (nothing is contacted)."""
    if str(cfg.get("provider") or "custom") not in PROVIDERS:
        return [f"unknown email provider {cfg.get('provider')!r}; one of {', '.join(PROVIDERS)}"]
    if provider(cfg)["kind"] == "graph":
        from .graph import validate as gv
        return gv(cfg) + ([] if secret else ["client secret is required"])
    from .email import validate as ev
    return ev(apply_preset(cfg))


def make_channel(cfg: dict[str, Any], secret: str | None) -> Any:
    if str(cfg.get("provider") or "custom") not in PROVIDERS:
        raise ConfigError(f"unknown email provider {cfg.get('provider')!r}; one of {', '.join(PROVIDERS)}")
    if provider(cfg)["kind"] == "graph":
        from .graph import GraphMailChannel
        return GraphMailChannel(cfg, secret)
    from .email import EmailChannel
    return EmailChannel(apply_preset(cfg), secret)


def catalog() -> list[dict[str, Any]]:
    """For the UI's provider picker."""
    return [{"id": k, **v} for k, v in PROVIDERS.items()]
