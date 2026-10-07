"""Create or update the Conditional Access policy for the agent (CA_POLICY_NAME in .env).

The policy requires MFA whenever a user signs in to use the agent, i.e. requests a token for
the agent's API, which is exposed by the agent identity blueprint
(api://<blueprint appId>/access_as_user). Sign-in frequency "Every time" makes Entra ask for a
full sign-in (password + MFA) on each run instead of reusing an earlier session (Entra doesn't
re-prompt more than once every 5 minutes).

For on-behalf-of (OBO) agents the token subject is the user, so Conditional Access targets
users and the agent's resource, not the agent identity. See:
https://learn.microsoft.com/entra/identity/conditional-access/agent-id

Run `python setup/admin_login.py` and `python setup/provision_entra.py` first.
Use --report-only to create the policy in report-only mode instead of enforcing it, and
--no-reauth to allow reusing a recent MFA instead of prompting every time.
"""
from __future__ import annotations

import argparse
import os

from graph_admin import graph
from provision_entra import BLUEPRINT_NAME, ENV_PATH, read_env

POLICY_NAME = os.environ.get("CA_POLICY_NAME") or "Spain-News-Agent-CA"


def upsert(body: dict) -> str:
    existing = [
        p
        for p in graph("GET", "/identity/conditionalAccess/policies?$select=id,displayName")["value"]
        if p["displayName"] == POLICY_NAME
    ]
    if existing:
        policy_id = existing[0]["id"]
        graph("PATCH", f"/identity/conditionalAccess/policies/{policy_id}", body)
        print(f"Updated policy '{POLICY_NAME}' ({policy_id})")
        return policy_id
    policy_id = graph("POST", "/identity/conditionalAccess/policies", body)["id"]
    print(f"Created policy '{POLICY_NAME}' ({policy_id})")
    return policy_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report-only", action="store_true", help="create the policy in report-only mode")
    parser.add_argument("--no-reauth", action="store_true", help="don't force a fresh MFA on every sign-in")
    args = parser.parse_args()

    env = read_env(ENV_PATH)
    blueprint_app_id = env.get("BLUEPRINT_APP_ID")
    if not blueprint_app_id:
        raise SystemExit(f"BLUEPRINT_APP_ID missing from {os.path.basename(ENV_PATH)}; run provision_entra.py first")

    sd = graph("GET", "/policies/identitySecurityDefaultsEnforcementPolicy?$select=isEnabled")
    if sd.get("isEnabled"):
        raise SystemExit("Security defaults are enabled; Conditional Access policies can't be used until they're disabled.")

    body = {
        "displayName": POLICY_NAME,
        "state": "enabledForReportingButNotEnforced" if args.report_only else "enabled",
        "conditions": {
            "clientAppTypes": ["all"],
            "users": {"includeUsers": ["All"]},
            "applications": {"includeApplications": [blueprint_app_id]},
        },
        "grantControls": {"operator": "OR", "builtInControls": ["mfa"]},
        # "Every time" only supports full reauthentication (password + MFA), not MFA-only.
        "sessionControls": None
        if args.no_reauth
        else {
            "signInFrequency": {
                "isEnabled": True,
                "frequencyInterval": "everyTime",
                "authenticationType": "primaryAndSecondaryAuthentication",
            }
        },
    }
    policy_id = upsert(body)

    policy = graph("GET", f"/identity/conditionalAccess/policies/{policy_id}", retry_not_found=True)
    conditions = policy["conditions"]
    frequency = (policy.get("sessionControls") or {}).get("signInFrequency") or {}
    print(f"  State           : {policy['state']}")
    print(f"  Users           : {conditions['users']['includeUsers']}")
    print(f"  Target resource : {BLUEPRINT_NAME} ({conditions['applications']['includeApplications']})")
    print(f"  Grant           : {policy['grantControls']['operator']} {policy['grantControls']['builtInControls']}")
    if frequency.get("isEnabled"):
        print(f"  Sign-in freq.   : {frequency.get('frequencyInterval')} ({frequency.get('authenticationType')})")
    else:
        print("  Sign-in freq.   : not set (a recent MFA can be reused)")


if __name__ == "__main__":
    main()
