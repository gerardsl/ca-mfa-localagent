"""Offline tests for attribute-policy creation and switching; no Entra or LLM calls."""
from __future__ import annotations

import copy
import io
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))

import create_attribute_ca_policy as attribute_policy
import graph_admin
from create_ca_policy import build_policy

TENANT = "00000000-0000-0000-0000-000000000001"
BLUEPRINT = "00000000-0000-0000-0000-000000000002"
PRINCIPAL = "00000000-0000-0000-0000-000000000003"
POLICY_ID = "00000000-0000-0000-0000-000000000004"
POLICY_NAME = "Example-Attribute-MFA"
SETTINGS = {"AZURE_TENANT_ID": TENANT, "BLUEPRINT_APP_ID": BLUEPRINT}


class FakeGraph:
    def __init__(self, existing=None, values=None):
        self.policy = copy.deepcopy(existing)
        self.principal = {
            "@odata.type": "#microsoft.graph.agentIdentityBlueprintPrincipal",
            "id": PRINCIPAL,
            "appId": BLUEPRINT,
            "displayName": "Example-Agent-Blueprint",
            "customSecurityAttributes": {
                "CompanyOrg": {"Org": ["IT"] if values is None else values}
            },
        }
        self.calls = []
        self.policy_reads = 0
        self.concurrent_change = False
        self.duplicate = False

    @property
    def writes(self):
        return [(method, path, body) for method, path, body in self.calls if method != "GET"]

    def __call__(self, method, path, body=None, **kwargs):
        if kwargs.get("scopes") != graph_admin.ATTRIBUTE_SCOPES:
            raise AssertionError("Attribute requests must opt in to the required permissions.")
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/policies/identitySecurityDefaultsEnforcementPolicy":
            return {"isEnabled": False}
        if method == "GET" and path.startswith("/directory/customSecurityAttributeDefinitions/"):
            return {"type": "String", "isCollection": True}
        if method == "GET" and path.startswith("/identity/conditionalAccess/policies?"):
            matches = [{"id": POLICY_ID, "displayName": POLICY_NAME}] if self.policy else []
            return {"value": matches * (2 if self.duplicate else 1)}
        if method == "GET" and path.startswith("/servicePrincipals"):
            return copy.deepcopy(self.principal)
        if method == "PATCH" and path == f"/servicePrincipals/{PRINCIPAL}":
            self.principal["customSecurityAttributes"].update(copy.deepcopy(body["customSecurityAttributes"]))
            return None
        if path == "/identity/conditionalAccess/policies" and method == "POST":
            self.policy = {"id": POLICY_ID, **copy.deepcopy(body)}
            return {"id": POLICY_ID}
        if path == f"/identity/conditionalAccess/policies/{POLICY_ID}":
            if method == "GET":
                self.policy_reads += 1
                if self.concurrent_change and self.policy_reads == 2:
                    self.policy["state"] = "disabled"
                return copy.deepcopy(self.policy)
            if method == "PATCH":
                # Graph returns default/null properties even when omitted from a PATCH.
                self.policy["conditions"].update(copy.deepcopy(body["conditions"]))
                self.policy["state"] = body["state"]
                return None
        raise AssertionError(f"Unexpected Graph request: {method} {path}")


class AttributePolicyTests(unittest.TestCase):
    def run_command(self, fake, *args, token_tenant=TENANT):
        with ExitStack() as stack:
            stack.enter_context(patch.object(attribute_policy, "read_env", return_value=SETTINGS))
            stack.enter_context(patch.object(attribute_policy, "get_token", return_value="stub-token"))
            stack.enter_context(patch.object(attribute_policy, "decode_jwt", return_value={"tid": token_tenant}))
            stack.enter_context(patch.object(attribute_policy, "graph", side_effect=fake))
            stack.enter_context(patch.object(attribute_policy.time, "sleep"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            attribute_policy.main(["--policy-name", POLICY_NAME, *args])

    def existing_policy(self):
        policy = build_policy(
            POLICY_NAME,
            attribute_policy.application_assignments("agent-only", "CompanyOrg", "Org", "IT"),
        )
        policy["id"] = POLICY_ID
        policy["conditions"]["users"]["excludeUsers"] = ["example-emergency-account"]
        policy["conditions"]["locations"] = {"includeLocations": ["All"], "excludeLocations": ["example-office"]}
        policy["conditions"]["devices"] = None
        policy["sessionControls"]["persistentBrowser"] = {"isEnabled": True, "mode": "never"}
        return policy

    def test_new_policies_use_the_selected_scope_and_start_report_only(self):
        cases = [
            ("agent-only", ["AllAgentIdResources"], "exclude", '-notContains "IT"'),
            ("attribute-only", [], "include", '-contains "IT"'),
        ]
        for mode, resources, filter_mode, comparison in cases:
            with self.subTest(mode=mode):
                fake = FakeGraph()
                self.run_command(fake, "--mode", mode)
                self.assertEqual(fake.policy["state"], attribute_policy.REPORT_ONLY)
                self.assertEqual(fake.policy["conditions"]["applications"], {
                    "includeApplications": resources,
                    "excludeApplications": [],
                    "applicationFilter": {
                        "mode": filter_mode,
                        "rule": f"CustomSecurityAttribute.CompanyOrg_Org {comparison}",
                    },
                })
                self.assertEqual(fake.policy["grantControls"]["builtInControls"], ["mfa"])
                self.assertEqual(
                    fake.policy["sessionControls"]["signInFrequency"]["frequencyInterval"], "everyTime"
                )
                self.assertEqual([method for method, _, _ in fake.writes], ["POST"])

    def test_switching_preserves_existing_restrictions_and_enabled_state(self):
        original = self.existing_policy()
        fake = FakeGraph(existing=original)
        self.run_command(fake, "--mode", "attribute-only")
        expected = copy.deepcopy(original)
        expected["conditions"]["applications"] = attribute_policy.application_assignments(
            "attribute-only", "CompanyOrg", "Org", "IT"
        )
        self.assertEqual(fake.policy, expected)
        self.assertEqual([method for method, _, _ in fake.writes], ["PATCH"])

        self.run_command(fake, "--mode", "agent-only")
        self.assertEqual(fake.policy, original)

    def test_state_changes_require_an_explicit_option(self):
        enabled = FakeGraph()
        self.run_command(enabled, "--mode", "agent-only", "--enable")
        self.assertEqual(enabled.policy["state"], "enabled")

        reporting = FakeGraph(existing=self.existing_policy())
        self.run_command(reporting, "--mode", "agent-only", "--report-only")
        self.assertEqual(reporting.policy["state"], attribute_policy.REPORT_ONLY)

    def test_tagging_is_opt_in_and_preserves_existing_values(self):
        fake = FakeGraph(values=["Finance"])
        with self.assertRaisesRegex(SystemExit, "--tag-blueprint"):
            self.run_command(fake, "--mode", "agent-only")
        self.assertEqual(fake.writes, [])

        self.run_command(fake, "--mode", "agent-only", "--tag-blueprint")
        self.assertEqual(
            fake.principal["customSecurityAttributes"]["CompanyOrg"]["Org"], ["Finance", "IT"]
        )
        self.assertEqual([method for method, _, _ in fake.writes], ["PATCH", "POST"])

        fake.calls.clear()
        self.run_command(fake, "--mode", "agent-only", "--tag-blueprint")
        self.assertFalse(any(path.startswith("/servicePrincipals") for _, path, _ in fake.writes))

    def test_cannot_treat_missing_attribute_permissions_as_an_empty_assignment(self):
        fake = FakeGraph()
        del fake.principal["customSecurityAttributes"]
        with self.assertRaisesRegex(SystemExit, "Graph did not return"):
            self.run_command(fake, "--mode", "agent-only", "--tag-blueprint")
        self.assertEqual(fake.writes, [])

    def test_wrong_tenant_is_rejected_before_any_graph_request(self):
        fake = FakeGraph()
        with self.assertRaisesRegex(SystemExit, "does not belong"):
            self.run_command(fake, "--mode", "agent-only", token_tenant="different-tenant")
        self.assertEqual(fake.calls, [])

    def test_rejects_a_regular_client_instead_of_the_blueprint_principal(self):
        fake = FakeGraph()
        fake.principal["@odata.type"] = "#microsoft.graph.servicePrincipal"
        with self.assertRaisesRegex(SystemExit, "blueprint principal"):
            self.run_command(fake, "--mode", "agent-only", "--tag-blueprint")
        self.assertEqual(fake.writes, [])

    def test_duplicate_or_incompatible_policies_are_not_overwritten(self):
        duplicate = FakeGraph(existing=self.existing_policy())
        duplicate.duplicate = True
        with self.assertRaisesRegex(SystemExit, "Multiple policies"):
            self.run_command(duplicate, "--mode", "agent-only")
        self.assertEqual(duplicate.writes, [])

        incompatible = FakeGraph(existing=self.existing_policy())
        incompatible.policy["grantControls"]["builtInControls"] = ["block"]
        with self.assertRaisesRegex(SystemExit, "expected MFA/everyTime"):
            self.run_command(incompatible, "--mode", "attribute-only")
        self.assertEqual(incompatible.writes, [])

    def test_concurrent_changes_are_not_overwritten(self):
        fake = FakeGraph(existing=self.existing_policy())
        fake.concurrent_change = True
        with self.assertRaisesRegex(SystemExit, "changed during setup"):
            self.run_command(fake, "--mode", "attribute-only")
        self.assertEqual(fake.writes, [])

    def test_filter_values_cannot_inject_a_second_rule(self):
        for flag, value in (
            ("--attribute-value", 'IT" -or true'),
            ("--attribute-set", "CompanyOrg)"),
        ):
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                fake = FakeGraph()
                with self.assertRaises(SystemExit) as error:
                    self.run_command(fake, "--mode", "agent-only", flag, value)
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(fake.calls, [])


class AdminScopeTests(unittest.TestCase):
    def test_cached_tokens_use_optional_attribute_scopes_without_changing_defaults(self):
        for scopes in (None, graph_admin.ATTRIBUTE_SCOPES):
            with self.subTest(attribute_permissions=scopes is not None):
                app = Mock()
                app.get_accounts.return_value = [{"realm": graph_admin.TENANT_ID, "username": "admin@example.test"}]
                app.acquire_token_silent.return_value = {"access_token": "stub-token"}
                with (
                    patch.object(graph_admin, "_app", return_value=(app, Mock())),
                    patch.object(graph_admin, "_save"),
                    patch.object(graph_admin, "ADMIN_UPN", ""),
                ):
                    self.assertEqual(graph_admin.get_token(scopes=scopes), "stub-token")
                requested = app.acquire_token_silent.call_args.args[0]
                self.assertEqual(requested, graph_admin.SCOPES if scopes is None else scopes)

    def test_shared_policy_builder_preserves_no_reauthentication_behavior(self):
        policy = build_policy("Example-MFA", {"includeApplications": [BLUEPRINT]}, reauthenticate=False)
        self.assertEqual(policy["state"], "enabled")
        self.assertIsNone(policy["sessionControls"])
        self.assertEqual(policy["grantControls"]["builtInControls"], ["mfa"])


if __name__ == "__main__":
    unittest.main()
