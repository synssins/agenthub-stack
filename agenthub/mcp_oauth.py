"""Authorise an OAuth-protected MCP server (Settings > MCP servers) for AgentHub.

  python3 mcp_oauth.py start <server>          -> prints a login URL
  python3 mcp_oauth.py finish <server> '<url>' -> paste the 127.0.0.1 URL the browser landed on

The browser ends on a dead http://127.0.0.1:8765/callback page (no listener needed);
its address holds the code. Tokens go into the server's "oauth" entry in
data/mcp_servers.json; mcp_hub refreshes them before use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).with_name("data")
REDIRECT = "http://127.0.0.1:8765/callback"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _post(url: str, data: dict, as_json: bool = False) -> dict:
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    hdr = {"Content-Type": "application/json"} if as_json else {}
    with urllib.request.urlopen(urllib.request.Request(url, body, hdr), timeout=30) as r:
        return json.load(r)


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _server(data: Path, name: str) -> dict:
    srv = next((s for s in json.loads((data / "mcp_servers.json").read_text("utf-8")) if s["name"] == name), None)
    if not srv or srv.get("transport", "http") != "http":
        raise ValueError(f"no HTTP MCP server named {name}")
    return srv


def start(name: str, data: Path = DATA) -> str:
    """Register a client with the server's auth server; -> the login URL to open."""
    url = _server(data, name)["url"]
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
    try:
        prm = _get(origin + "/.well-known/oauth-protected-resource")
    except Exception:
        raise ValueError(f"{name} does not advertise OAuth (no /.well-known/oauth-protected-resource)")
    asm = _get(prm["authorization_servers"][0].rstrip("/") + "/.well-known/oauth-authorization-server")
    client = _post(asm["registration_endpoint"], {
        "client_name": "AgentHub", "redirect_uris": [REDIRECT], "response_types": ["code"],
        "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none"}, True)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    scope = " ".join(s for s in prm.get("scopes_supported", []) if s in ("openid", "profile", "email", "offline_access"))
    _write(data / f"oauth_pending_{name}.json", {
        "client_id": client["client_id"], "token_endpoint": asm["token_endpoint"],
        "verifier": verifier, "state": state, "resource": prm.get("resource", url)})
    return (asm["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "scope": scope, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": prm.get("resource", url)}))


def finish(name: str, landed: str, data: Path = DATA) -> str:
    """Exchange the code in the URL the browser landed on; store tokens; -> status line."""
    pend_path = data / f"oauth_pending_{name}.json"
    if not pend_path.exists():
        raise ValueError("no sign-in in progress: start again")
    pend = json.loads(pend_path.read_text("utf-8"))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(landed).query)
    if q.get("state", [""])[0] != pend["state"]:
        raise ValueError("that URL is from a different sign-in: start again")
    if "error" in q:
        raise ValueError(f"authorisation failed: {q['error'][0]} {q.get('error_description', [''])[0]}")
    if "code" not in q:
        raise ValueError("no code in that URL: paste the full address the browser landed on")
    tok = _post(pend["token_endpoint"], {
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": pend["client_id"], "code_verifier": pend["verifier"], "resource": pend["resource"]})
    if "refresh_token" not in tok:
        raise ValueError("no refresh token issued; the token would expire with no way to renew it")
    servers = data / "mcp_servers.json"
    items = json.loads(servers.read_text("utf-8"))
    for s in items:
        if s["name"] == name:
            s["oauth"] = {"client_id": pend["client_id"], "token_endpoint": pend["token_endpoint"],
                          "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                          "expires_at": int(time.time()) + int(tok.get("expires_in", 3600))}
    _write(servers, items)
    pend_path.unlink()
    return (f"{name}: authorised, access token valid {tok.get('expires_in', '?')}s, refresh token stored")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "start":
        print(start(sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "finish":
        print(finish(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(__doc__)
