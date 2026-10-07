"""Shared helpers for the admin (setup) scripts in this folder.

The admin signs in with the device code flow using the Microsoft Graph Command Line Tools public
client (the same client `Connect-MgGraph` uses). The tenant comes from AZURE_TENANT_ID in .env; when
it's empty, any work or school account can sign in ("organizations") and admin_login.py saves that
account's tenant to .env. Tokens are cached under %LOCALAPPDATA%, outside the project folder.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import msal
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = str(ROOT / ".env")
ENV_EXAMPLE_PATH = str(ROOT / ".env.example")
load_dotenv(ENV_PATH)

TENANT_ID = os.environ.get("AZURE_TENANT_ID", "").strip()
ADMIN_UPN = os.environ.get("ADMIN_UPN", "").strip()  # optional: which cached account to use
GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

CACHE_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ca-mfa-localagent",
    "admin_token_cache.json",
)

# Requested once at sign-in so provisioning, consent grants and the CA policy
# can all be done without signing in again.
SCOPES = [
    "User.Read",
    "AgentIdentityBlueprint.Create",
    "AgentIdentityBlueprint.ReadWrite.All",
    "AgentIdentityBlueprint.AddRemoveCreds.All",
    "AgentIdentityBlueprint.UpdateAuthProperties.All",
    "AgentIdentityBlueprintPrincipal.Create",
    "AgentIdentity.Create.All",
    "AgentIdentity.ReadWrite.All",
    "Application.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All",
    "Policy.Read.All",
    "Policy.ReadWrite.ConditionalAccess",
    "AuditLog.Read.All",
    "Directory.Read.All",
]


class GraphError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, text: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {text}")
        self.status = status
        self.text = text


def read_env(path: str = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    values[m.group(1)] = m.group(2).strip("'\"")
    return values


def update_env(path: str, values: dict[str, str]) -> None:
    """Set keys in .env, keeping every other line. Creates .env from .env.example if needed."""
    if not os.path.exists(path) and os.path.exists(ENV_EXAMPLE_PATH):
        shutil.copyfile(ENV_EXAMPLE_PATH, path)
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    missing = [k for k in values if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{k}={values[k]}" for k in missing)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def _app(tenant: str | None = None) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache.deserialize(f.read())
    authority = f"https://login.microsoftonline.com/{tenant or TENANT_ID or 'organizations'}"
    app = msal.PublicClientApplication(GRAPH_CLI_CLIENT_ID, authority=authority, token_cache=cache)
    return app, cache


def _save(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def login_device_code(tenant: str | None = None) -> dict:
    app, cache = _app(tenant)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(f"Could not start the device code flow: {json.dumps(flow, indent=2)}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
    _save(cache)
    return result


def get_token() -> str:
    app, cache = _app()
    accounts = app.get_accounts()
    if TENANT_ID:
        accounts = [a for a in accounts if a.get("realm") in (TENANT_ID, None)] or accounts
    if ADMIN_UPN:
        accounts = [a for a in accounts if a.get("username", "").lower() == ADMIN_UPN.lower()]
    if not accounts:
        raise SystemExit("No cached admin sign-in. Run: python setup/admin_login.py")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        error = (result or {}).get("error_description") or (result or {}).get("error")
        raise SystemExit(f"Silent token acquisition failed ({error}). Run: python setup/admin_login.py")
    _save(cache)
    return result["access_token"]


def decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def graph(
    method: str,
    path: str,
    body: Any = None,
    *,
    beta: bool = False,
    retry_not_found: bool = False,
    max_attempts: int = 8,
) -> Any:
    """Call Microsoft Graph. Retries throttling/transient errors and, when
    `retry_not_found` is set, replication delays right after object creation."""
    url = path if path.startswith("https://") else (GRAPH_BETA if beta else GRAPH) + path
    for attempt in range(1, max_attempts + 1):
        headers = {
            "Authorization": f"Bearer {get_token()}",
            "OData-Version": "4.0",
            "Content-Type": "application/json",
            "ConsistencyLevel": "eventual",
        }
        resp = requests.request(method, url, headers=headers, json=body, timeout=60)
        if resp.ok:
            return resp.json() if resp.content else None
        text = resp.text.lower()
        retryable = resp.status_code in (429, 500, 502, 503, 504)
        if retry_not_found and resp.status_code in (400, 403, 404) and (
            "not found" in text or "does not exist" in text or "notfound" in text
        ):
            retryable = True
        if not retryable or attempt == max_attempts:
            raise GraphError(method, url, resp.status_code, resp.text)
        delay = int(resp.headers.get("Retry-After") or 0) or min(5 * attempt, 30)
        print(f"  ...HTTP {resp.status_code} from Graph, retrying in {delay}s", flush=True)
        time.sleep(delay)
    raise AssertionError("unreachable")
