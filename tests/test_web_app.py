"""Integration test for the web UI (web_app.py).

Entra is stubbed where a real user would be needed (the code redemption, the OBO exchange and
Graph /me); the news feeds and Mistral are called for real. Covers the sign-in redirect (PKCE,
form_post), the callback (state checks, replay, session cookie), CSRF and Host checks, the
streamed top-5 digest, a follow-up chat question and sign-out.

Needs a configured .env (see .env.example) and internet access.
Usage: python tests/test_web_app.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import web_app  # noqa: E402


def fake_jwt(claims: dict) -> str:
    def segment(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


GRAPH_CLAIMS = {
    "app_displayname": os.environ.get("AGENT_IDENTITY_NAME") or "Spain-News-Agent-Identity",
    "appid": os.environ.get("AGENT_IDENTITY_APP_ID"),
    "upn": "test.user@contoso.example",
    "scp": "User.Read profile openid email",
    "amr": ["pwd", "mfa"],
}
obo_calls: list[str] = []
web_app.AUTH.exchange_on_behalf_of = lambda token, scope=None: obo_calls.append(token) or fake_jwt(GRAPH_CLAIMS)

BASE = f"http://localhost:{web_app.PORT}"
client = web_app.app.test_client()
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def stream_events(response) -> list[dict]:
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


# Anonymous access
r = client.get("/", base_url=BASE)
check("GET / serves the page", r.status_code == 200 and b"Spain News Agent" in r.data)
check("CSP header present", "script-src 'self'" in r.headers.get("Content-Security-Policy", ""))
r = client.get("/", base_url=f"http://evil.example:{web_app.PORT}")
check("foreign Host rejected", r.status_code == 400)
r = client.get("/api/me", base_url=BASE)
check("GET /api/me without session -> 401", r.status_code == 401)
r = client.post("/api/news", base_url=BASE)
check("POST /api/news without session -> 401", r.status_code == 401)

# Sign-in redirect
r = client.get("/login", base_url=BASE)
location = urlparse(r.headers.get("Location", ""))
query = parse_qs(location.query)
check("GET /login redirects to Entra", r.status_code == 302 and location.netloc == "login.microsoftonline.com")
check("  client_id = agent client app", query.get("client_id", [""])[0] == os.environ["AGENT_CLIENT_APP_ID"])
check("  redirect_uri = local server", query.get("redirect_uri", [""])[0] == web_app.REDIRECT_URI)
check("  scope has the agent scope", os.environ["AGENT_SCOPE"] in query.get("scope", [""])[0])
check("  PKCE S256", query.get("code_challenge_method", [""])[0] == "S256")
check("  response_mode=form_post", query.get("response_mode", [""])[0] == "form_post")
state = query.get("state", [""])[0]

# Callback errors
r = client.post("/", base_url=BASE, data={"code": "bogus", "state": "wrong"})
check("callback with unknown state -> error", r.status_code == 302 and "signin_error" in r.headers["Location"])
r = client.post("/", base_url=BASE, data={"error": "access_denied", "error_description": "AADSTS50076: MFA required.\nTrace", "state": state})
check("callback with Entra error -> shown to the user", "AADSTS50076" in r.headers.get("Location", ""))

# Successful callback (code redemption and Graph /me stubbed)
r = client.get("/login", base_url=BASE)
state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
user_token = fake_jwt({"exp": int(time.time()) + 3600, "aud": os.environ["BLUEPRINT_APP_ID"]})
web_app.MSAL_APP.acquire_token_by_auth_code_flow = lambda flow, response: (
    {"access_token": user_token} if response.get("state") == flow["state"] else {"error": "bad_state"}
)
real_get = web_app.requests.get


class FakeGraphResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"displayName": "Test User", "userPrincipalName": GRAPH_CLAIMS["upn"]}


web_app.requests.get = lambda url, *a, **kw: FakeGraphResponse() if url == web_app.GRAPH_ME else real_get(url, *a, **kw)
r = client.post("/", base_url=BASE, data={"code": "fake-code", "state": state})
cookie = r.headers.get("Set-Cookie", "")
check("callback success -> 303 to / with session cookie", r.status_code == 303 and r.headers["Location"] == "/" and "sna_session=" in cookie)
check("  session cookie HttpOnly + SameSite=Lax", "HttpOnly" in cookie and "SameSite=Lax" in cookie)
r = client.post("/", base_url=BASE, data={"code": "fake-code", "state": state})
check("the same state can't be replayed", r.status_code == 302 and "signin_error" in r.headers.get("Location", ""))

# Signed-in API
r = client.get("/api/me", base_url=BASE)
me = r.get_json() or {}
check("GET /api/me signed in -> 200 with MFA", r.status_code == 200 and me.get("mfa") is True, json.dumps(me.get("agent")))
r = client.post("/api/news", base_url=BASE, json={})
check("POST /api/news without CSRF token -> 403", r.status_code == 403)

started = time.time()
r = client.post("/api/news", base_url=BASE, json={}, headers={"X-CSRF-Token": me.get("csrf", "")})
events = stream_events(r)
logs = [e["message"] for e in events if e["type"] == "log"]
news = next((e for e in events if e["type"] == "news"), None)
errors = [e for e in events if e["type"] in ("error", "auth_error")]
check("POST /api/news streams progress + 5 stories", news is not None and len(news["stories"]) == 5,
      f"{len(logs)} progress events, {time.time() - started:.1f}s, errors={errors}")
check("  OBO happens before the agent runs", bool(obo_calls) and bool(logs) and "OBO" in logs[0], logs[0] if logs else "")
for i, story in enumerate((news or {}).get("stories", []), 1):
    print(f"      {i}. {story['headline'][:90]}  [{story['article']['source']}]")

started = time.time()
r = client.post("/api/chat", base_url=BASE, json={"message": "Tell me more about story 2"},
                headers={"X-CSRF-Token": me.get("csrf", "")})
answer = next((e for e in stream_events(r) if e["type"] == "answer"), None)
check("POST /api/chat answers with sources", answer is not None and bool(answer["text"]),
      f"{time.time() - started:.1f}s, {len(answer['sources']) if answer else 0} sources")
if answer:
    print(f"      {answer['text'][:300]}")

r = client.post("/api/chat", base_url=BASE, json={"message": ""}, headers={"X-CSRF-Token": me.get("csrf", "")})
check("empty chat message -> 400", r.status_code == 400)
r = client.post("/logout", base_url=BASE, headers={"X-CSRF-Token": me.get("csrf", "")})
check("POST /logout -> 200", r.status_code == 200)
r = client.get("/api/me", base_url=BASE)
check("GET /api/me after sign-out -> 401", r.status_code == 401)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
