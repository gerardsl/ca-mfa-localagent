"""Provision the Microsoft Entra Agent ID objects for the Spain News Agent in your tenant.

Idempotent: safe to re-run. Creates or updates (names can be changed in .env):
  * Agent identity blueprint  BLUEPRINT_NAME (+ its blueprint principal)
      - exposes api://<blueprint appId>/access_as_user so users can call the agent (OBO)
      - a client secret for local development, written to .env
  * Agent identity            AGENT_IDENTITY_NAME (child of the blueprint)
      - admin-consented delegated Microsoft Graph permission: User.Read
  * Public client app         CLIENT_APP_NAME (the agent's user sign-in front end)
      - pre-authorized for the blueprint's access_as_user scope

Run `python setup/admin_login.py` first.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from graph_admin import ENV_PATH, GraphError, graph, read_env, update_env

BLUEPRINT_NAME = os.environ.get("BLUEPRINT_NAME") or "Spain-News-Agent-Blueprint"
AGENT_IDENTITY_NAME = os.environ.get("AGENT_IDENTITY_NAME") or "Spain-News-Agent-Identity"
CLIENT_APP_NAME = os.environ.get("CLIENT_APP_NAME") or "Spain-News-Agent-Client"
SCOPE_VALUE = "access_as_user"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
AGENT_GRAPH_SCOPES = {"User.Read"}
SECRET_NAME = "ca-mfa-localagent local dev"
SECRET_LIFETIME_DAYS = 180


def first(path: str) -> dict | None:
    items = graph("GET", path).get("value", [])
    if len(items) > 1:
        print(f"  WARNING: {len(items)} objects matched {path}; using the first one.")
    return items[0] if items else None


def ensure_delegated_grant(client_sp_id: str, resource_sp_id: str, scopes: set[str], label: str) -> None:
    grants = graph(
        "GET", f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp_id}'", retry_not_found=True
    ).get("value", [])
    existing = next(
        (g for g in grants if g["resourceId"] == resource_sp_id and g["consentType"] == "AllPrincipals"), None
    )
    if existing:
        current = set(existing.get("scope", "").split())
        if scopes <= current:
            print(f"  = {label}: already granted ({' '.join(sorted(current))})")
            return
        graph("PATCH", f"/oauth2PermissionGrants/{existing['id']}", {"scope": " ".join(sorted(current | scopes))})
        print(f"  ~ {label}: updated to {' '.join(sorted(current | scopes))}")
        return
    body = {
        "clientId": client_sp_id,
        "consentType": "AllPrincipals",
        "resourceId": resource_sp_id,
        "scope": " ".join(sorted(scopes)),
    }
    expiry = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        graph("POST", "/oauth2PermissionGrants", {**body, "expiryTime": expiry}, retry_not_found=True)
    except GraphError as e:
        if e.status != 400 or "expirytime" not in e.text.lower():
            raise
        graph("POST", "/oauth2PermissionGrants", body, retry_not_found=True)
    print(f"  + {label}: granted {' '.join(sorted(scopes))} (admin consent for all users)")


def main() -> None:
    me = graph("GET", "/me?$select=id,displayName,userPrincipalName")
    tenant = graph("GET", "/organization?$select=id,displayName")["value"][0]
    tenant_id = tenant["id"]
    user_ref = f"https://graph.microsoft.com/v1.0/users/{me['id']}"
    print(f"Tenant {tenant['displayName']} ({tenant_id}); owner/sponsor: {me['userPrincipalName']}")

    # 1. Agent identity blueprint
    print(f"\n[1/7] Agent identity blueprint '{BLUEPRINT_NAME}'")
    bp = first(f"/applications/microsoft.graph.agentIdentityBlueprint?$filter=displayName eq '{BLUEPRINT_NAME}'")
    if bp is None:
        bp = graph(
            "POST",
            "/applications/microsoft.graph.agentIdentityBlueprint",
            {
                "displayName": BLUEPRINT_NAME,
                "sponsors@odata.bind": [user_ref],
                "owners@odata.bind": [user_ref],
            },
        )
        print(f"  + created appId={bp['appId']}")
    else:
        print(f"  = exists appId={bp['appId']}")
    bp_obj_id, bp_app_id = bp["id"], bp["appId"]

    # 2. Blueprint principal (never auto-created)
    print("\n[2/7] Blueprint principal")
    bp_sp = first(f"/servicePrincipals?$filter=appId eq '{bp_app_id}'&$select=id,appId")
    if bp_sp is None:
        bp_sp = graph(
            "POST",
            "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal",
            {"appId": bp_app_id},
            retry_not_found=True,
        )
        print(f"  + created id={bp_sp['id']}")
    else:
        print(f"  = exists id={bp_sp['id']}")

    # 3. Expose the blueprint as an API so users can sign in to the agent (OBO audience)
    print(f"\n[3/7] Expose api://{bp_app_id}/{SCOPE_VALUE}")
    api = (graph("GET", f"/applications/{bp_obj_id}?$select=identifierUris,api", retry_not_found=True) or {})
    identifier_uris = api.get("identifierUris") or []
    api_cfg = api.get("api") or {}
    scopes = api_cfg.get("oauth2PermissionScopes") or []
    scope = next((s for s in scopes if s.get("value") == SCOPE_VALUE), None)
    scope_id = scope["id"] if scope else str(uuid.uuid4())
    if scope is None:
        scopes = scopes + [
            {
                "id": scope_id,
                "value": SCOPE_VALUE,
                "type": "User",
                "isEnabled": True,
                "adminConsentDisplayName": "Use the Spain News Agent",
                "adminConsentDescription": "Allows the app to call the Spain News Agent on behalf of the signed-in user.",
                "userConsentDisplayName": "Use the Spain News Agent",
                "userConsentDescription": "Allows the app to call the Spain News Agent on your behalf.",
            }
        ]
    app_id_uri = f"api://{bp_app_id}"
    if scope is None or app_id_uri not in identifier_uris or api_cfg.get("requestedAccessTokenVersion") != 2:
        graph(
            "PATCH",
            f"/applications/{bp_obj_id}",
            {
                "identifierUris": sorted(set(identifier_uris) | {app_id_uri}),
                "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes},
            },
            retry_not_found=True,
        )
        print(f"  + configured identifier URI and scope (id={scope_id})")
    else:
        print(f"  = already configured (scope id={scope_id})")

    # 4. Public client app the user signs in with (the agent's front end)
    print(f"\n[4/7] Client app '{CLIENT_APP_NAME}'")
    client_body = {
        "displayName": CLIENT_APP_NAME,
        "signInAudience": "AzureADMyOrg",
        "isFallbackPublicClient": True,
        "publicClient": {"redirectUris": ["http://localhost"]},
        "requiredResourceAccess": [
            {"resourceAppId": bp_app_id, "resourceAccess": [{"id": scope_id, "type": "Scope"}]},
        ],
    }
    client = first(f"/applications?$filter=displayName eq '{CLIENT_APP_NAME}'&$select=id,appId")
    if client is None:
        client = graph("POST", "/applications", client_body)
        print(f"  + created appId={client['appId']}")
        try:
            graph(
                "POST",
                f"/applications/{client['id']}/owners/$ref",
                {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{me['id']}"},
                retry_not_found=True,
            )
        except GraphError as e:
            if "already exist" not in e.text.lower():
                raise
    else:
        graph("PATCH", f"/applications/{client['id']}", client_body)
        print(f"  = exists appId={client['appId']} (settings refreshed)")
    client_app_id = client["appId"]
    client_sp = first(f"/servicePrincipals?$filter=appId eq '{client_app_id}'&$select=id")
    if client_sp is None:
        client_sp = graph("POST", "/servicePrincipals", {"appId": client_app_id}, retry_not_found=True)
        print(f"  + service principal id={client_sp['id']}")

    # Pre-authorize the client on the blueprint scope (no consent prompt) and admin-consent it.
    api = graph("GET", f"/applications/{bp_obj_id}?$select=api")["api"]
    pre = api.get("preAuthorizedApplications") or []
    if not any(p["appId"] == client_app_id and scope_id in (p.get("delegatedPermissionIds") or []) for p in pre):
        pre = [p for p in pre if p["appId"] != client_app_id] + [
            {"appId": client_app_id, "delegatedPermissionIds": [scope_id]}
        ]
        graph(
            "PATCH",
            f"/applications/{bp_obj_id}",
            {
                "api": {
                    "requestedAccessTokenVersion": 2,
                    "oauth2PermissionScopes": api.get("oauth2PermissionScopes") or [],
                    "preAuthorizedApplications": pre,
                }
            },
            retry_not_found=True,
        )
        print("  + pre-authorized on the blueprint's access_as_user scope")
    else:
        print("  = already pre-authorized on the blueprint")
    ensure_delegated_grant(client_sp["id"], bp_sp["id"], {SCOPE_VALUE}, "client -> blueprint")

    # 5. Agent identity (child of the blueprint)
    print(f"\n[5/7] Agent identity '{AGENT_IDENTITY_NAME}'")
    agent = first(f"/servicePrincipals/microsoft.graph.agentIdentity?$filter=displayName eq '{AGENT_IDENTITY_NAME}'")
    if agent is None:
        agent = graph(
            "POST",
            "/servicePrincipals/microsoft.graph.agentIdentity",
            {
                "displayName": AGENT_IDENTITY_NAME,
                "agentIdentityBlueprintId": bp_app_id,
                "sponsors@odata.bind": [user_ref],
                "owners@odata.bind": [user_ref],
            },
            retry_not_found=True,
        )
        print(f"  + created appId={agent['appId']} id={agent['id']}")
    else:
        print(f"  = exists appId={agent['appId']} id={agent['id']}")
    if agent.get("agentIdentityBlueprintId") not in (None, bp_app_id):
        raise SystemExit(f"Agent identity belongs to another blueprint: {agent.get('agentIdentityBlueprintId')}")

    # 6. Delegated Graph permission the agent identity may use on behalf of users
    print("\n[6/7] Delegated permissions for the agent identity")
    graph_sp = first(f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'&$select=id")
    ensure_delegated_grant(agent["id"], graph_sp["id"], AGENT_GRAPH_SCOPES, "agent identity -> Microsoft Graph")

    # 7. Blueprint credential for local development (secrets can only live on the blueprint)
    print("\n[7/7] Blueprint client secret")
    env = read_env(ENV_PATH)
    secret = env.get("BLUEPRINT_CLIENT_SECRET") if env.get("BLUEPRINT_APP_ID") == bp_app_id else None
    if secret:
        print("  = reusing the secret already stored in .env")
    else:
        end = (datetime.now(timezone.utc) + timedelta(days=SECRET_LIFETIME_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cred = graph(
            "POST",
            f"/applications/{bp_obj_id}/addPassword",
            {"passwordCredential": {"displayName": SECRET_NAME, "endDateTime": end}},
            retry_not_found=True,
        )
        secret = cred["secretText"]
        print(f"  + added secret '{SECRET_NAME}' (expires {end})")

    update_env(
        ENV_PATH,
        {
            "AZURE_TENANT_ID": tenant_id,
            "AGENT_CLIENT_APP_ID": client_app_id,
            "BLUEPRINT_APP_ID": bp_app_id,
            "BLUEPRINT_CLIENT_SECRET": secret,
            "AGENT_IDENTITY_APP_ID": agent["appId"],
            "AGENT_SCOPE": f"api://{bp_app_id}/{SCOPE_VALUE}",
        },
    )
    print("\nWrote Entra settings to .env")
    print(f"  Blueprint            {BLUEPRINT_NAME}: appId={bp_app_id} objectId={bp_obj_id} principalId={bp_sp['id']}")
    print(f"  Agent identity       {AGENT_IDENTITY_NAME}: appId={agent['appId']} id={agent['id']}")
    print(f"  Client (front end)   {CLIENT_APP_NAME}: appId={client_app_id}")


if __name__ == "__main__":
    main()
