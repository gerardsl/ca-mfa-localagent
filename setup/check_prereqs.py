"""Check tenant prerequisites for Agent ID + Conditional Access (read-only).

Shows the signed-in admin and directory roles, whether security defaults are off (required for
Conditional Access), licenses with Entra ID P1/P2 and Agent 365 plans, the existing Conditional
Access policies and whether this project's objects already exist.

Run `python setup/admin_login.py` first.
"""
from __future__ import annotations

from create_ca_policy import POLICY_NAME
from graph_admin import graph
from provision_entra import AGENT_IDENTITY_NAME, BLUEPRINT_NAME, CLIENT_APP_NAME


def main() -> None:
    tenant = graph("GET", "/organization?$select=id,displayName")["value"][0]
    me = graph("GET", "/me?$select=displayName,userPrincipalName")
    roles = graph("GET", "/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=displayName")["value"]
    print(f"Tenant {tenant['displayName']} ({tenant['id']})")
    print(f"Admin: {me['displayName']} <{me['userPrincipalName']}>")
    print(f"  Directory roles: {', '.join(sorted(r['displayName'] for r in roles)) or 'none'}")

    defaults = graph("GET", "/policies/identitySecurityDefaultsEnforcementPolicy?$select=isEnabled")
    print(f"\nSecurity defaults enabled: {defaults['isEnabled']}  (must be False to use Conditional Access)")

    print("\nLicenses with Entra ID P1/P2 or Agent 365 plans:")
    for sku in graph("GET", "/subscribedSkus?$select=skuPartNumber,capabilityStatus,servicePlans")["value"]:
        plans = sorted(
            p["servicePlanName"]
            for p in sku["servicePlans"]
            if "AAD_PREMIUM" in p["servicePlanName"] or "AGENT" in p["servicePlanName"].upper()
        )
        if plans:
            print(f"  {sku['skuPartNumber']} [{sku['capabilityStatus']}]: {', '.join(plans)}")

    policies = graph("GET", "/identity/conditionalAccess/policies?$select=displayName,state")["value"]
    print(f"\nConditional Access policies ({len(policies)}):")
    for policy in policies:
        print(f"  [{policy['state']}] {policy['displayName']}")

    print("\nThis project's objects:")
    lookups = [
        ("Blueprint", f"/applications/microsoft.graph.agentIdentityBlueprint?$filter=displayName eq '{BLUEPRINT_NAME}'"),
        ("Client app", f"/applications?$filter=displayName eq '{CLIENT_APP_NAME}'"),
        ("Agent identity", f"/servicePrincipals/microsoft.graph.agentIdentity?$filter=displayName eq '{AGENT_IDENTITY_NAME}'"),
    ]
    for label, path in lookups:
        found = graph("GET", path + "&$select=appId")["value"]
        print(f"  {label:<15} {('exists, appId ' + found[0]['appId']) if found else 'not created yet'}")
    exists = any(p["displayName"] == POLICY_NAME for p in policies)
    print(f"  {'CA policy':<15} {'exists' if exists else 'not created yet'}")


if __name__ == "__main__":
    main()
