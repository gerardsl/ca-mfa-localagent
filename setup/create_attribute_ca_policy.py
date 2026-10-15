"""Create or switch between two attribute-based MFA policy options in your own tenant.

New policies start in report-only mode. Existing compatible policies keep their state and
non-resource settings unless --enable or --report-only is explicitly supplied.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import time
from functools import partial

from create_ca_policy import build_policy
from graph_admin import ATTRIBUTE_SCOPES, ENV_PATH, GraphError, decode_jwt, get_token, graph, read_env

REPORT_ONLY = "enabledForReportingButNotEnforced"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
        raise argparse.ArgumentTypeError("Use a letter followed by letters, numbers or underscores.")
    return value


def attribute_value(value: str) -> str:
    if not value.strip() or any(char in value for char in '"\\') or any(ord(char) < 32 for char in value):
        raise argparse.ArgumentTypeError("Use a nonempty value without quotes, backslashes or control characters.")
    return value


def application_assignments(mode: str, attribute_set: str, attribute_name: str, value: str) -> dict:
    agent_only = mode == "agent-only"
    operator = "-notContains" if agent_only else "-contains"
    return {
        "includeApplications": ["AllAgentIdResources"] if agent_only else [],
        "excludeApplications": [],
        "applicationFilter": {
            "mode": "exclude" if agent_only else "include",
            "rule": f"CustomSecurityAttribute.{attribute_set}_{attribute_name} {operator} {json.dumps(value, ensure_ascii=False)}",
        },
    }


def attribute_values(principal: dict, attribute_set: str, attribute_name: str) -> list[str]:
    if "customSecurityAttributes" not in principal:
        raise SystemExit("Graph did not return customSecurityAttributes; check the attribute permissions and role.")
    values = ((principal["customSecurityAttributes"] or {}).get(attribute_set) or {}).get(attribute_name, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise SystemExit("The assigned attribute must be a collection of strings.")
    return values


def writable(value):
    if isinstance(value, dict):
        return {key: writable(item) for key, item in value.items() if "@odata" not in key and item is not None}
    if isinstance(value, list):
        return [writable(item) for item in value]
    return value


def require_mfa_policy(policy: dict) -> None:
    frequency = (policy.get("sessionControls") or {}).get("signInFrequency") or {}
    if not (
        (policy.get("grantControls") or {}).get("builtInControls") == ["mfa"]
        and frequency.get("isEnabled")
        and frequency.get("frequencyInterval") == "everyTime"
        and frequency.get("authenticationType") == "primaryAndSecondaryAuthentication"
    ):
        raise SystemExit(
            "The existing policy does not have the expected MFA/everyTime controls. "
            "Choose a different --policy-name or review it manually; it was not overwritten."
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["agent-only", "attribute-only"])
    parser.add_argument(
        "--policy-name", default=os.environ.get("ATTRIBUTE_CA_POLICY_NAME") or "Spain-News-Agent-Attribute-CA"
    )
    parser.add_argument("--attribute-set", type=identifier, default="CompanyOrg")
    parser.add_argument("--attribute-name", type=identifier, default="Org")
    parser.add_argument("--attribute-value", type=attribute_value, default="IT")
    parser.add_argument("--tag-blueprint", action="store_true", help="also assign the value to the blueprint principal")
    state_args = parser.add_mutually_exclusive_group()
    state_args.add_argument("--enable", action="store_true", help="enforce the policy after reviewing its scope")
    state_args.add_argument("--report-only", action="store_true", help="explicitly leave the policy in report-only mode")
    args = parser.parse_args(argv)

    env = read_env(ENV_PATH)
    tenant = env.get("AZURE_TENANT_ID")
    blueprint_app_id = env.get("BLUEPRINT_APP_ID")
    if any(not value or value.startswith("<") for value in (tenant, blueprint_app_id)):
        raise SystemExit("Run setup/admin_login.py and setup/provision_entra.py in your own tenant first.")
    claims = decode_jwt(get_token(scopes=ATTRIBUTE_SCOPES))
    if claims.get("tid", "").lower() != tenant.lower():
        raise SystemExit("The admin token does not belong to AZURE_TENANT_ID. Sign in to the configured tenant again.")
    call = partial(graph, scopes=ATTRIBUTE_SCOPES)
    if call("GET", "/policies/identitySecurityDefaultsEnforcementPolicy")["isEnabled"]:
        raise SystemExit("Security defaults are enabled. No attributes or policies were changed.")

    definition = call(
        "GET", f"/directory/customSecurityAttributeDefinitions/{args.attribute_set}_{args.attribute_name}"
    )
    if definition.get("type") != "String" or definition.get("isCollection") is not True:
        raise SystemExit("Create a String attribute with 'Allow multiple values to be assigned' enabled first.")

    matches = []
    path = "/identity/conditionalAccess/policies?$select=id,displayName"
    while path:
        page = call("GET", path)
        matches.extend(policy for policy in page["value"] if policy["displayName"] == args.policy_name)
        path = page.get("@odata.nextLink")
    if len(matches) > 1:
        raise SystemExit("Multiple policies have this name. Resolve the duplicate names before continuing.")
    existing = call("GET", f"/identity/conditionalAccess/policies/{matches[0]['id']}") if matches else None
    if existing:
        require_mfa_policy(existing)

    select = "?$select=id,appId,displayName,customSecurityAttributes"
    blueprint = call("GET", f"/servicePrincipals(appId='{blueprint_app_id}')" + select)
    if blueprint.get("@odata.type") != "#microsoft.graph.agentIdentityBlueprintPrincipal":
        raise SystemExit("BLUEPRINT_APP_ID must identify an agent identity blueprint principal, not the client app.")
    values = attribute_values(blueprint, args.attribute_set, args.attribute_name)
    if args.attribute_value not in values:
        if not args.tag_blueprint:
            raise SystemExit(
                "The blueprint principal does not have the requested attribute value. "
                "Assign it yourself or rerun with --tag-blueprint."
            )
        call(
            "PATCH",
            f"/servicePrincipals/{blueprint['id']}",
            {
                "customSecurityAttributes": {
                    args.attribute_set: {
                        "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
                        f"{args.attribute_name}@odata.type": "#Collection(String)",
                        args.attribute_name: values + [args.attribute_value],
                    }
                }
            },
        )
        for attempt in range(12):
            tagged = call("GET", f"/servicePrincipals/{blueprint['id']}" + select)
            if args.attribute_value in attribute_values(tagged, args.attribute_set, args.attribute_name):
                break
            time.sleep(min(attempt + 2, 10))
        else:
            raise SystemExit("The attribute write was accepted but has not replicated. No policy was written.")
        print(f"Assigned {args.attribute_set}.{args.attribute_name}={args.attribute_value} to the blueprint principal.")

    applications = application_assignments(args.mode, args.attribute_set, args.attribute_name, args.attribute_value)
    if args.enable:
        state = "enabled"
    elif args.report_only or not existing:
        state = REPORT_ONLY
    else:
        state = existing["state"]
    if existing:
        conditions = writable(copy.deepcopy(existing["conditions"]))
        conditions["applications"] = applications
        policy_id = existing["id"]
        current = call("GET", f"/identity/conditionalAccess/policies/{policy_id}")
        if any(current.get(key) != existing.get(key) for key in ("conditions", "grantControls", "sessionControls", "state")):
            raise SystemExit("The policy changed during setup. It was not overwritten; run the command again.")
        call("PATCH", f"/identity/conditionalAccess/policies/{policy_id}", {"conditions": conditions, "state": state})
        action = "Updated"
    else:
        body = build_policy(args.policy_name, applications, state=state)
        # A failed POST can have committed on the server; a rerun will look up the name before retrying.
        policy_id = call("POST", "/identity/conditionalAccess/policies", body, max_attempts=1)["id"]
        action = "Created"

    for attempt in range(12):
        policy = call("GET", f"/identity/conditionalAccess/policies/{policy_id}", retry_not_found=True)
        actual = policy["conditions"]["applications"]
        if policy["state"] == state and all(actual.get(key) == value for key, value in applications.items()):
            require_mfa_policy(policy)
            if existing:
                before = {key: value for key, value in existing["conditions"].items() if key != "applications"}
                after = {key: value for key, value in policy["conditions"].items() if key != "applications"}
                if before != after or any(
                    policy.get(key) != existing.get(key) for key in ("grantControls", "sessionControls")
                ):
                    raise SystemExit("Policy resource targeting was updated, but unrelated settings differed on read-back.")
            break
        time.sleep(min(attempt + 2, 10))
    else:
        raise SystemExit("The policy write was accepted but read-back has not converged. Review it before enabling.")

    print(f"{action} '{args.policy_name}' ({policy_id})")
    print(f"  State: {policy['state']}")
    print(f"  Mode: {args.mode}")
    print(f"  Resource selection: {applications['includeApplications'] or 'attribute filter only'}")
    print(f"  Filter: {applications['applicationFilter']['mode']} {applications['applicationFilter']['rule']}")
    print("  Grant: MFA; sign-in frequency: Every time (primary and secondary authentication)")
    if args.mode == "attribute-only":
        print("  Scope includes non-agent enterprise applications with the same attribute value.")


if __name__ == "__main__":
    try:
        main()
    except GraphError as error:
        raise SystemExit(str(error)) from error
