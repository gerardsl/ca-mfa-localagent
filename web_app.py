"""Web UI for the Spain News Agent.

Run `python web_app.py` and a browser opens at http://localhost:5050. Users sign in with
Microsoft Entra ID through the agent's client app (created by setup/provision_entra.py), so the
Conditional Access policy from setup/create_ca_policy.py applies (password + MFA). Tokens and the
Mistral key stay on this local server; the browser only gets an HttpOnly session cookie. Before
every agent action, the agent identity performs the OBO exchange on behalf of the signed-in user.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

import msal
import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from agent import MistralChat, SpainNewsAgent, digest_text
from entra_auth import AgentIdentityAuth, AuthError, decode_jwt
from news_tools import Article, NewsDesk

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
load_dotenv(os.path.join(ROOT, ".env"))

PORT = int(os.environ.get("WEB_PORT", "5050"))
# Matches the client app's public client redirect URI http://localhost (Entra ignores the port).
REDIRECT_URI = f"http://localhost:{PORT}"
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
SESSION_COOKIE = "sna_session"
MAX_QUESTION_LENGTH = 500
GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName"

try:
    AUTH = AgentIdentityAuth.from_env()
except AuthError as e:
    raise SystemExit(str(e))
if not os.environ.get("MISTRAL_API_KEY") or os.environ["MISTRAL_API_KEY"].startswith("<"):
    raise SystemExit("Set MISTRAL_API_KEY in .env (copy .env.example to .env first).")
MSAL_APP = msal.PublicClientApplication(
    AUTH.client_app_id, authority=f"https://login.microsoftonline.com/{AUTH.tenant_id}"
)
LLM = MistralChat(os.environ.get("MISTRAL_API_KEY", ""), os.environ.get("MISTRAL_MODEL", "mistral-small-latest"))

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="/static")
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # don't log URLs that carry auth codes


@dataclass
class WebSession:
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    user_token: str | None = None
    user_claims: dict = field(default_factory=dict)
    agent_claims: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    desk: NewsDesk = field(default_factory=NewsDesk)
    history: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def signed_in(self) -> bool:
        return bool(self.user_token) and self.user_claims.get("exp", 0) > time.time() + 60


SESSIONS: dict[str, WebSession] = {}
SESSIONS_LOCK = threading.Lock()
# Pending sign-ins keyed by their single-use OAuth state. The form_post callback is a cross-site
# POST from Entra, which doesn't carry the SameSite=Lax session cookie.
PENDING_LOGINS: dict[str, tuple[float, dict]] = {}
PENDING_LOGIN_TTL = 15 * 60


def log_event(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def current_session() -> WebSession | None:
    return SESSIONS.get(request.cookies.get(SESSION_COOKIE, ""))


def new_session(replacing: str | None = None, session: WebSession | None = None) -> tuple[str, WebSession]:
    sid = secrets.token_urlsafe(32)
    session = session or WebSession()
    with SESSIONS_LOCK:
        if replacing:
            SESSIONS.pop(replacing, None)
        SESSIONS[sid] = session
    return sid, session


def with_session_cookie(resp: Response, sid: str) -> Response:
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="Lax", path="/")
    return resp


def sign_in_error(message: str) -> Response:
    log_event(f"[auth] sign-in failed: {message}")
    return redirect("/?signin_error=" + quote(message[:300]))


def authorize_agent(sess: WebSession, log: Callable[[str], None] | None = None, load_profile: bool = False) -> str:
    """OBO: the agent identity gets a Microsoft Graph token on behalf of the signed-in user."""
    if not sess.signed_in:
        raise AuthError("Your sign-in expired. Please sign in again.")
    graph_token = AUTH.exchange_on_behalf_of(sess.user_token)
    sess.agent_claims = decode_jwt(graph_token)
    if load_profile:
        resp = requests.get(GRAPH_ME, headers={"Authorization": f"Bearer {graph_token}"}, timeout=30)
        resp.raise_for_status()
        sess.profile = resp.json()
    if log:
        log(
            f"Agent identity {sess.agent_claims.get('app_displayname', 'agent')} got a token on behalf of "
            f"{sess.agent_claims.get('upn', 'you')} (OBO)"
        )
    return graph_token


def guarded_session() -> tuple[WebSession | None, tuple[Response, int] | None]:
    sess = current_session()
    if sess is None or not sess.signed_in:
        return None, (jsonify(error="Your sign-in expired. Please sign in again."), 401)
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), sess.csrf):
        return None, (jsonify(error="Invalid CSRF token."), 403)
    return sess, None


def stream_job(sess: WebSession, work: Callable[[Callable[[str], None]], dict]) -> Response | tuple[Response, int]:
    """Run an agent job in a thread and stream its progress to the browser as NDJSON events."""
    if not sess.lock.acquire(blocking=False):
        return jsonify(error="The agent is already working on a request."), 409
    events: queue.Queue = queue.Queue()

    def log(message: str) -> None:
        events.put({"type": "log", "message": message.strip().lstrip("- ").strip()})

    def runner() -> None:
        try:
            events.put(work(log))
        except AuthError as e:
            events.put({"type": "auth_error", "message": str(e)})
        except Exception as e:  # report any agent failure to the page instead of a broken stream
            log_event(f"[agent] error: {e}")
            events.put({"type": "error", "message": str(e)})
        finally:
            sess.lock.release()
            events.put(None)

    threading.Thread(target=runner, daemon=True).start()

    def generate():
        while (event := events.get()) is not None:
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


def article_json(article: Article) -> dict:
    return {
        "id": article.id,
        "title": article.title,
        "source": article.source,
        "published": article.published.isoformat() if article.published else None,
        "link": article.link if article.link.startswith("https://") else None,
    }


@app.before_request
def check_host():
    # Rejects DNS-rebinding requests that reach this local server under a foreign host name.
    if request.host not in ALLOWED_HOSTS:
        return Response("Invalid host", status=400)
    return None


@app.after_request
def security_headers(resp: Response) -> Response:
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        return finish_sign_in(request.form.to_dict())
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/login")
def login():
    # response_mode=form_post keeps the authorization code out of URLs (history, logs, referrers).
    flow = MSAL_APP.initiate_auth_code_flow(
        [AUTH.agent_scope], redirect_uri=REDIRECT_URI, prompt="select_account", response_mode="form_post"
    )
    now = time.time()
    with SESSIONS_LOCK:
        for state in [s for s, (created, _) in PENDING_LOGINS.items() if now - created > PENDING_LOGIN_TTL]:
            del PENDING_LOGINS[state]
        PENDING_LOGINS[flow["state"]] = (now, flow)
    log_event("[auth] sign-in started")
    return redirect(flow["auth_uri"])


def finish_sign_in(auth_response: dict) -> Response:
    with SESSIONS_LOCK:
        pending = PENDING_LOGINS.pop(auth_response.get("state", ""), None)
    if pending is None or time.time() - pending[0] > PENDING_LOGIN_TTL:
        return sign_in_error("The sign-in session expired or didn't match. Please try again.")
    flow = pending[1]
    if "error" in auth_response:
        description = auth_response.get("error_description") or auth_response["error"]
        return sign_in_error(description.splitlines()[0])
    try:
        result = MSAL_APP.acquire_token_by_auth_code_flow(flow, auth_response)
    except ValueError:
        return sign_in_error("The sign-in response didn't match the request. Please try again.")
    if "access_token" not in result:
        description = (result.get("error_description") or "").splitlines()
        return sign_in_error(f"{result.get('error')}: {description[0] if description else ''}")

    sess = WebSession(user_token=result["access_token"])
    sess.user_claims = decode_jwt(sess.user_token)
    try:
        authorize_agent(sess, load_profile=True)
    except (AuthError, requests.RequestException) as e:
        return sign_in_error(f"The agent identity couldn't act on your behalf: {e}")

    log_event(
        f"[auth] signed in: {sess.profile.get('userPrincipalName')} "
        f"(amr: {', '.join(sess.agent_claims.get('amr', []))}); agent identity: "
        f"{sess.agent_claims.get('app_displayname')}"
    )
    # A brand-new session id is issued after sign-in (prevents session fixation).
    sid, _ = new_session(replacing=request.cookies.get(SESSION_COOKIE), session=sess)
    return with_session_cookie(redirect("/", code=303), sid)


@app.get("/api/me")
def api_me():
    sess = current_session()
    if sess is None or not sess.signed_in:
        return jsonify(error="not_signed_in"), 401
    methods = sess.agent_claims.get("amr") or []
    return jsonify(
        csrf=sess.csrf,
        user={"name": sess.profile.get("displayName"), "upn": sess.profile.get("userPrincipalName")},
        auth_methods=methods,
        mfa="mfa" in methods,
        agent={
            "name": sess.agent_claims.get("app_displayname"),
            "app_id": sess.agent_claims.get("appid"),
            "scopes": sess.agent_claims.get("scp"),
        },
        expires=datetime.fromtimestamp(sess.user_claims["exp"], tz=timezone.utc).isoformat(),
    )


@app.post("/api/news")
def api_news():
    sess, error = guarded_session()
    if error:
        return error

    def work(log: Callable[[str], None]) -> dict:
        log_event(f"[agent] top 5 requested by {sess.profile.get('userPrincipalName')}")
        authorize_agent(sess, log)
        log(f"Checking the news online with Mistral model '{LLM.model}'")
        agent = SpainNewsAgent(LLM, sess.desk, log=log)
        picks = agent.run()
        if not picks:
            raise RuntimeError("The agent couldn't produce a news digest. Please try again.")
        sess.history = (
            sess.history
            + [
                {"role": "user", "content": "What are the top 5 news about Spain right now?"},
                {"role": "assistant", "content": digest_text(picks)},
            ]
        )[-12:]
        log_event(f"[agent] top {len(picks)} delivered")
        return {
            "type": "news",
            "stories": [
                {
                    "headline": str(item.get("headline", "")).strip(),
                    "summary": str(item.get("summary", "")).strip(),
                    "why_it_matters": str(item.get("why_it_matters") or "").strip(),
                    "article": article_json(article),
                }
                for item, article in picks
            ],
        }

    return stream_job(sess, work)


@app.post("/api/chat")
def api_chat():
    sess, error = guarded_session()
    if error:
        return error
    question = str((request.get_json(silent=True) or {}).get("message", "")).strip()
    if not question or len(question) > MAX_QUESTION_LENGTH:
        return jsonify(error=f"Ask a question of 1-{MAX_QUESTION_LENGTH} characters."), 400

    def work(log: Callable[[str], None]) -> dict:
        log_event(f"[agent] question from {sess.profile.get('userPrincipalName')}")
        authorize_agent(sess, log)
        agent = SpainNewsAgent(LLM, sess.desk, log=log)
        raw = agent.chat(question, sess.history)
        sess.history = (sess.history + [{"role": "user", "content": question}, {"role": "assistant", "content": raw}])[-12:]
        text, sources = agent.cite(raw)
        return {"type": "answer", "text": text, "sources": [article_json(a) for a in sources]}

    return stream_job(sess, work)


@app.post("/logout")
def logout():
    sid = request.cookies.get(SESSION_COOKIE, "")
    sess = SESSIONS.get(sid)
    if sess and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), sess.csrf):
        return jsonify(error="Invalid CSRF token."), 403
    with SESSIONS_LOCK:
        SESSIONS.pop(sid, None)
    log_event("[auth] signed out")
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def main() -> None:
    url = f"http://localhost:{PORT}"
    print(f"Spain News Agent web UI: {url}  (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
