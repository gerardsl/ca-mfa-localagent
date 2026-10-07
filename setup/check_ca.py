"""Verify Conditional Access for the agent.

1. What If evaluation: which policies apply when a user signs in to the agent
   (target resource = the agent identity blueprint).
2. Recent sign-ins to the agent from the sign-in logs, with the Conditional Access
   policies that were applied and the MFA requirement (logs can take a few minutes).

Usage: python setup/check_ca.py [--user someone@contoso.com]   (default: the signed-in admin)
"""
from __future__ import annotations

import argparse

from create_ca_policy import POLICY_NAME
from graph_admin import GraphError, graph
from provision_entra import ENV_PATH, read_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Conditional Access for the agent.")
    parser.add_argument("--user", help="user principal name to evaluate (default: the signed-in admin)")
    parser.add_argument("--top", type=int, default=5, help="number of recent sign-ins to show")
    args = parser.parse_args()

    env = read_env(ENV_PATH)
    blueprint_app_id, client_app_id = env["BLUEPRINT_APP_ID"], env["AGENT_CLIENT_APP_ID"]
    user_path = f"/users/{args.user}" if args.user else "/me"
    user = graph("GET", f"{user_path}?$select=id,userPrincipalName")

    print(f"What If: {user['userPrincipalName']} signs in to the agent (resource {blueprint_app_id})")
    result = graph(
        "POST",
        "/identity/conditionalAccess/evaluate",
        {
            "signInIdentity": {"@odata.type": "#microsoft.graph.userSignIn", "userId": user["id"]},
            "signInContext": {
                "@odata.type": "#microsoft.graph.applicationContext",
                "includeApplications": [blueprint_app_id],
            },
            "signInConditions": {"clientAppType": "mobileAppsAndDesktopClients", "devicePlatform": "windows"},
            "appliedPoliciesOnly": True,
        },
    )
    applied = [p for p in result.get("value", []) if p.get("policyApplies")]
    for policy in applied:
        grant = policy.get("grantControls") or {}
        frequency = (policy.get("sessionControls") or {}).get("signInFrequency") or {}
        reauth = f" sign-in frequency={frequency.get('frequencyInterval')}" if frequency.get("isEnabled") else ""
        marker = "  <== agent policy" if policy.get("displayName") == POLICY_NAME else ""
        print(f"  - {policy['displayName']} [{policy.get('state')}] grant={grant.get('builtInControls')}{reauth}{marker}")
    if not any(p.get("displayName") == POLICY_NAME for p in applied):
        print(f"  WARNING: '{POLICY_NAME}' does not apply to this sign-in.")

    print(f"\nRecent sign-ins to the agent (client app {client_app_id}):")
    try:
        sign_ins = graph(
            "GET",
            f"/auditLogs/signIns?$filter=appId eq '{client_app_id}'&$top={args.top}",
            beta=True,
        ).get("value", [])
    except GraphError as e:
        print(f"  Could not read sign-in logs: HTTP {e.status}")
        return
    if not sign_ins:
        print("  None yet (sign-in logs can take a few minutes to appear).")
    for s in sign_ins:
        status = s.get("status") or {}
        outcome = "success" if status.get("errorCode") == 0 else f"failed {status.get('errorCode')}: {status.get('failureReason')}"
        print(
            f"  {s['createdDateTime']}  {s.get('userPrincipalName')} -> {s.get('resourceDisplayName')}  "
            f"[{outcome}] auth requirement: {s.get('authenticationRequirement')}"
        )
        for p in s.get("appliedConditionalAccessPolicies") or []:
            if p.get("result") not in ("notApplied", "notEnabled"):
                print(f"      CA: {p.get('displayName')} -> {p.get('result')} {p.get('enforcedGrantControls')}")


if __name__ == "__main__":
    main()
