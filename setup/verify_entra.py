"""Verify the provisioned Agent ID objects and the first step of the agent token exchange.

Checks the blueprint (identifier URI, access_as_user scope, pre-authorized client), the agent
identity (type, parent blueprint, delegated grants), then requests the blueprint's exchange
token for the agent identity (client credentials + fmi_path), which needs no user.

Run `python setup/admin_login.py` and `python setup/provision_entra.py` first.
"""
from __future__ import annotations

import time

import requests

from graph_admin import decode_jwt, graph
from provision_entra import ENV_PATH, read_env


def main() -> None:
    env = read_env(ENV_PATH)
    blueprint = graph(
        "GET",
        f"/applications?$filter=appId eq '{env['BLUEPRINT_APP_ID']}'&$select=displayName,appId,identifierUris,api",
    )["value"][0]
    api = blueprint["api"]
    print(f"Blueprint {blueprint['displayName']} ({blueprint['appId']})")
    print(f"  identifierUris : {blueprint['identifierUris']}")
    print(f"  token version  : {api.get('requestedAccessTokenVersion')}")
    print(f"  scopes         : {[(s['value'], s['isEnabled']) for s in api['oauth2PermissionScopes']]}")
    print(f"  pre-authorized : {[p['appId'] for p in api.get('preAuthorizedApplications') or []]}")

    agent = graph(
        "GET", f"/servicePrincipals/microsoft.graph.agentIdentity?$filter=appId eq '{env['AGENT_IDENTITY_APP_ID']}'"
    )["value"][0]
    grants = graph("GET", f"/oauth2PermissionGrants?$filter=clientId eq '{agent['id']}'")["value"]
    print(f"\nAgent identity {agent['displayName']} ({agent['appId']})")
    print(f"  blueprint      : {agent.get('agentIdentityBlueprintId')}")
    print(f"  grants         : {[(g['consentType'], g['scope']) for g in grants]}")

    print("\nToken exchange step 1: blueprint secret + fmi_path=<agent identity>")
    for attempt in range(1, 9):
        resp = requests.post(
            f"https://login.microsoftonline.com/{env['AZURE_TENANT_ID']}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": env["BLUEPRINT_APP_ID"],
                "client_secret": env["BLUEPRINT_CLIENT_SECRET"],
                "scope": "api://AzureADTokenExchange/.default",
                "fmi_path": env["AGENT_IDENTITY_APP_ID"],
            },
            timeout=30,
        )
        body = resp.json()
        if resp.ok:
            claims = decode_jwt(body["access_token"])
            print(f"  OK  aud={claims.get('aud')}  azp={claims.get('azp') or claims.get('appid')}")
            print(f"      sub={claims.get('sub')}")
            return
        # A new secret or agent identity can take a little while to replicate.
        print(f"  attempt {attempt}: {body.get('error')}: {(body.get('error_description') or '')[:160]}")
        time.sleep(15)
    raise SystemExit("The blueprint couldn't get an exchange token for the agent identity.")


if __name__ == "__main__":
    main()
