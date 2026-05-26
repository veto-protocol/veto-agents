"""Magic-link auth for veto-agents.

This is the real auth flow — same one veto-ai.com uses for the web app. The
CLI implements the device-code pattern (like `gh auth login` / AWS CLI):

  1. CLI generates a random device_code.
  2. CLI POSTs /api/v1/auth/email/start with {email, device_code}.
     Backend creates a MagicLinkToken tagged with the device_code, emails
     a link to the user.
  3. User clicks the link → backend marks the token consumed and writes
     the resolved api_key / agent_id / client_id back onto the token row.
  4. CLI polls /api/v1/auth/cli/poll with {device_code} every ~2s. While
     the user hasn't clicked yet, server returns {status: "pending"}.
     Once consumed, server returns {status: "ready", api_key, agent_id,
     client_id} and the CLI saves those to config.

The `cli-demo/register/` endpoint we previously used is for the anonymous
`npx @veto-protocol/pay` first-run case (wallet-only, no email). Veto Agents
is a real-user product, so we use the real auth flow.
"""

from __future__ import annotations

import re
import secrets
import time
import webbrowser
from dataclasses import dataclass

import httpx


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(s: str) -> bool:
    return bool(_EMAIL_RE.match(s.strip())) and len(s.strip()) <= 254


# Map common email domains → their webmail UI. For corporate / custom
# domains we fall back to opening the domain root (which usually has a
# mail subdomain or a "login" link visible).
_WEBMAIL = {
    "gmail.com":        "https://mail.google.com",
    "googlemail.com":   "https://mail.google.com",
    "outlook.com":      "https://outlook.live.com",
    "hotmail.com":      "https://outlook.live.com",
    "live.com":         "https://outlook.live.com",
    "msn.com":          "https://outlook.live.com",
    "yahoo.com":        "https://mail.yahoo.com",
    "ymail.com":        "https://mail.yahoo.com",
    "icloud.com":       "https://www.icloud.com/mail",
    "me.com":           "https://www.icloud.com/mail",
    "mac.com":          "https://www.icloud.com/mail",
    "protonmail.com":   "https://mail.proton.me",
    "proton.me":        "https://mail.proton.me",
    "pm.me":            "https://mail.proton.me",
    "fastmail.com":     "https://app.fastmail.com",
    "hey.com":          "https://app.hey.com",
}


def webmail_url_for(email: str) -> str | None:
    """Return the webmail URL for *known* providers only (gmail, outlook,
    proton, etc.). Returns None for corporate / custom domains — we used
    to fall back to `https://<domain>`, but that opens the user's company
    website, not their inbox, which is worse than doing nothing."""
    try:
        domain = email.split("@", 1)[1].strip().lower()
    except IndexError:
        return None
    return _WEBMAIL.get(domain)


def open_inbox_for(email: str) -> str | None:
    """Best-effort open the user's webmail in a browser. Only attempts
    for known webmail providers — for corporate/custom domains we don't
    open anything (the user knows where their email is). Returns the
    URL we attempted to open, or None if the domain isn't recognized."""
    url = webmail_url_for(email)
    if url is None:
        return None
    try:
        webbrowser.open(url)
    except Exception:
        pass  # best-effort; the caller will print the URL regardless
    return url


def generate_device_code() -> str:
    """A URL-safe random string within the server's accepted range (8–64 chars).

    Prefixed `dc_` so device codes are recognizable in logs (matches the
    convention used by the npm `@veto-protocol/cli` package).
    """
    return "dc_" + secrets.token_urlsafe(24)


@dataclass
class StartResult:
    expires_in: int  # seconds until the link expires (server-side TTL on the token)


@dataclass
class PollReady:
    api_key: str
    agent_id: str
    client_id: str


def start(
    *,
    api_base: str,
    email: str,
    device_code: str,
    timeout: float = 15.0,
) -> StartResult:
    """POST /api/v1/auth/email/start — triggers the email to be sent."""
    url = f"{api_base.rstrip('/')}/auth/email/start/"
    payload = {"email": email, "device_code": device_code}
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    return StartResult(expires_in=int(data.get("expires_in", 900)))


def poll_once(
    *,
    api_base: str,
    device_code: str,
    timeout: float = 10.0,
) -> PollReady | None:
    """One poll attempt. Returns PollReady when the user has clicked the link,
    or None if still pending. Raises on terminal errors (410 expired, etc.)."""
    url = f"{api_base.rstrip('/')}/auth/cli/poll/"
    payload = {"device_code": device_code}
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)

        if r.status_code == 410:
            raise TimeoutError("Magic link expired. Run setup again.")
        if r.status_code == 404:
            raise RuntimeError("device_code not found. Re-run setup.")
        r.raise_for_status()

        data = r.json()
    status = data.get("status")
    if status == "ready":
        return PollReady(
            api_key=data["api_key"],
            agent_id=data["agent_id"],
            client_id=data["client_id"],
        )
    return None  # pending


def poll_until_ready(
    *,
    api_base: str,
    device_code: str,
    interval_s: float = 2.0,
    timeout_s: float = 900.0,  # 15 min, matches MagicLinkToken.TOKEN_TTL server-side
    on_tick=None,
) -> PollReady:
    """Block until the user clicks the link, or raise after timeout_s.

    `on_tick(seconds_waited: int)` is invoked once per poll if provided —
    useful for showing a live spinner / elapsed-time counter in the CLI.
    """
    start_t = time.monotonic()
    while True:
        elapsed = int(time.monotonic() - start_t)
        if elapsed >= timeout_s:
            raise TimeoutError(f"Timed out after {timeout_s:.0f}s waiting for magic link.")

        result = poll_once(api_base=api_base, device_code=device_code)
        if result is not None:
            return result

        if on_tick is not None:
            on_tick(elapsed)
        time.sleep(interval_s)
