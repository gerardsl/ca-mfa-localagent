# Attribute-based MFA: two targeting options

This guide publishes two ways to require MFA for resources tagged `CompanyOrg.Org` with the value
`IT`. All examples use generic names. Sign in to **your own tenant**, provision your own objects, and
keep their IDs and credentials in your ignored `.env`.

Both options target **users accessing a resource**, not an autonomous agent authenticating without a
user. For this application's on-behalf-of (OBO) flow, the sign-in resource is the **agent identity
blueprint's service principal**. The child agent identity performs the later OBO exchange.

## Compare the options

| Setting | Option A: `agent-only` (conservative) | Option B: `attribute-only` (broader) |
| --- | --- | --- |
| Included resources | `AllAgentIdResources` | Selected by the attribute filter only; no explicit app IDs |
| Filter mode | **Exclude** | **Include** |
| Rule | `CustomSecurityAttribute.CompanyOrg_Org -notContains "IT"` | `CustomSecurityAttribute.CompanyOrg_Org -contains "IT"` |
| Explicit resource exclusions | None | None |
| Effect | Remove non-IT resources from the agent-resource scope | Select every matching resource, whether it is an agent or an ordinary enterprise application |

Both use all users, require MFA, and set sign-in frequency to **Every time**
(`primaryAndSecondaryAuthentication`).

### Why Option A uses a double negative

It starts with **All agent resources**, then **excludes resources that do not contain IT**. Thus
IT-tagged agent resources remain in scope. It does not exclude IT.

| Resource | Option A applies? | Option B applies? |
| --- | --- | --- |
| Agent sign-in resource tagged `Org = ["IT"]` | Yes | Yes |
| Agent sign-in resource tagged `Org = ["Finance", "IT"]` | Yes | Yes |
| Ordinary enterprise application tagged `Org = ["IT"]` | No | Yes |
| Resource with no `IT` assignment | No | No |

Option B is easier to read. It deliberately removes the agent-only boundary. Its
`includeApplications` collection is empty: do not add `All` and assume the positive filter narrows it.
The examples below use the exact resource-selection shapes tested for these two options.

## Prerequisites and permissions

Complete the normal own-tenant setup in the [README](../README.md) first. Check the current
[Conditional Access for agents licensing and requirements](https://learn.microsoft.com/en-us/entra/identity/conditional-access/agent-id).
Security defaults must already be off; this script does not disable them.

In **Entra ID > Custom security attributes**, create or reuse an active definition:

| Property | Example |
| --- | --- |
| Attribute set | `CompanyOrg` |
| Attribute | `Org` |
| Type | `String` |
| Allow multiple values to be assigned | Yes |
| Predefined value, if values are restricted | `IT` |

Creating the definition requires **Attribute Definition Administrator**. For the script, the signed-in
admin needs **Conditional Access Administrator**, **Attribute Definition Reader**, and the appropriate
attribute assignment access. Adding a tag with `--tag-blueprint` requires **Attribute Assignment
Administrator** for the attribute set. **Global Administrator alone does not grant custom-attribute
access.**

The optional sign-in flag requests these additional delegated Microsoft Graph permissions, on top of
the normal setup permissions:

```powershell
python setup\admin_login.py --attributes
# Optional tenant selection:
# python setup\admin_login.py --attributes --tenant contoso.onmicrosoft.com
```

- `CustomSecAttributeAssignment.ReadWrite.All`: read assignments and optionally tag the blueprint.
- `CustomSecAttributeDefinition.Read.All`: validate the attribute's type and collection setting.

Consent to these permissions using an authorized admin account. As with the normal setup, the admin
token cache is local, outside the repository; delete it when finished.

## Tag the resource that users actually sign into

For this application, tag the blueprint's **enterprise application / service principal**, not just the
child agent identity. A tag on a child identity is not automatically copied to its blueprint.

```powershell
python setup\create_attribute_ca_policy.py --mode agent-only --tag-blueprint --report-only
```

`--tag-blueprint` is explicit opt-in: it reads the blueprint principal identified by `BLUEPRINT_APP_ID`
in your `.env`, appends the selected value while retaining other assigned values, and verifies the
write. Without the flag, a missing assignment stops setup instead of silently creating a policy that
does not cover the local app. The script does not create definitions or tag child identities.

If the blueprint's UI does not expose an attribute assignment control, Microsoft Graph supports the
underlying service principal. The corresponding request is:

```http
PATCH https://graph.microsoft.com/v1.0/servicePrincipals/{blueprint-principal-object-id}
Content-Type: application/json
OData-Version: 4.0

{
  "customSecurityAttributes": {
    "CompanyOrg": {
      "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
      "Org@odata.type": "#Collection(String)",
      "Org": ["IT"]
    }
  }
}
```

This minimal body assumes no other `Org` values are assigned. When making a manual request, include
existing values too; the script handles the merge. Look up the principal with
`GET /servicePrincipals(appId='{BLUEPRINT_APP_ID}')`; its object ID is different from the blueprint
application object's ID.

**Scope limitation:** if multiple child agent identities share the same tagged blueprint as their
sign-in audience, the policy covers all of them. It cannot distinguish children by their own tags
during that inbound user sign-in.

## Option A: agent-only scope

The default attribute policy name is `Spain-News-Agent-Attribute-CA`, separate from the original
single-resource policy. New attribute policies start in report-only mode.

```powershell
python setup\create_attribute_ca_policy.py --mode agent-only --tag-blueprint --report-only
python setup\check_ca.py --policy-name Spain-News-Agent-Attribute-CA
# After reviewing What If and sign-in results:
python setup\create_attribute_ca_policy.py --mode agent-only --enable
```

The resource assignment sent to Graph is:

```json
{
  "includeApplications": ["AllAgentIdResources"],
  "excludeApplications": [],
  "applicationFilter": {
    "mode": "exclude",
    "rule": "CustomSecurityAttribute.CompanyOrg_Org -notContains \"IT\""
  }
}
```

In the portal, this reads as **Include: All agent resources** and an **Exclude** attribute filter with
the **Not contains** operator.

## Option B: positive inclusion, no exclusions

Use the same policy name to switch that policy, or a different `--policy-name` to create an independent
report-only comparison without changing an enforced policy.

```powershell
python setup\create_attribute_ca_policy.py --mode attribute-only --report-only
python setup\check_ca.py --policy-name Spain-News-Agent-Attribute-CA
# This also enforces MFA for ordinary enterprise applications tagged IT:
python setup\create_attribute_ca_policy.py --mode attribute-only --enable
```

The resource assignment is:

```json
{
  "includeApplications": [],
  "excludeApplications": [],
  "applicationFilter": {
    "mode": "include",
    "rule": "CustomSecurityAttribute.CompanyOrg_Org -contains \"IT\""
  }
}
```

In the portal, use a resource **Include** filter with **Contains: IT**, and no exclusions.
Do not select an agent identity as the policy subject: it cannot complete a human MFA challenge.

To switch the same policy back to the conservative form:

```powershell
python setup\create_attribute_ca_policy.py --mode agent-only --report-only
# Review, then enable:
python setup\create_attribute_ca_policy.py --mode agent-only --enable
```

Changing an existing policy to `--report-only` stops its enforcement while you test. Without an
explicit state flag, an existing compatible policy keeps its current state, including `enabled`.
The script preserves its users, user exclusions, other conditions, MFA settings and session controls
when switching modes. It refuses duplicate names or an existing policy without the expected
MFA/Every-time controls. It does not delete or change other policies.

To customize names or attributes:

```powershell
python setup\create_attribute_ca_policy.py --mode agent-only --policy-name Example-Attribute-MFA --attribute-set CompanyOrg --attribute-name Org --attribute-value IT --report-only
```

`ATTRIBUTE_CA_POLICY_NAME` in your local `.env` changes the default policy name. Always use that same
name when checking it with `check_ca.py --policy-name`.

## Verify the scope, not just whether an MFA prompt appears

Check the named policy with **What If** for a tagged agent resource and for a known untagged resource.
For Option A, also check that a tagged ordinary enterprise application is outside the scope.

```powershell
# Defaults to your local blueprint resource and the signed-in admin:
python setup\check_ca.py --policy-name Spain-News-Agent-Attribute-CA

# Select a resource you have independently checked is not tagged IT:
python setup\check_ca.py --policy-name Spain-News-Agent-Attribute-CA --resource-app-id "<untagged-resource-app-id>"
```

The development checks verified matching and nonmatching filters, including changing a report-only
test filter to a unique nonmatching value. Rerun the checks in your tenant: **read-back confirms the
stored configuration; What If checks targeting; a real sign-in checks the end-to-end experience.**

Other Conditional Access policies are cumulative. The original single-resource MFA policy and any
tenant-wide policy still apply independently. An MFA prompt alone does not prove which policy caused
it; inspect the named policy's What If result and the sign-in log's applied policies.

To test the local UI, sign out at `http://localhost:5050` and sign in again. **Every time** means fresh
authentication when Entra evaluates the session, not on every chat message. Entra allows a
five-minute reauthentication tolerance. Passwordless MFA methods can satisfy the requirement; it does
not necessarily mean a separate password prompt followed by a second factor.

Conditional Access protects Entra token acquisition. The public news feeds and Mistral API-key calls
are not themselves Entra-protected resources. The application's authenticated entry point gates those
actions.

## Offline tests and cleanup

```powershell
python -m unittest discover -s tests -p test_attribute_ca_policy.py -v
```

These tests make no Entra or LLM calls. They cover both assignment shapes, report-only defaults, state
changes, existing-policy preservation, opt-in tagging and its value merge, tenant and permission
guards, concurrent changes, and unchanged default authentication scopes.

After administration, remove the specific cache file created by the setup helper:

```powershell
Remove-Item -LiteralPath "$env:LOCALAPPDATA\ca-mfa-localagent\admin_token_cache.json"
```

Do not commit `.env`, tokens, sign-in logs or exported policies containing tenant-specific information.

## References

- [Conditional Access for agents](https://learn.microsoft.com/en-us/entra/identity/conditional-access/agent-id)
- [Filter for applications](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-filter-for-applications)
- [Target resources and All agent resources](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-cloud-apps)
- [Update service principal custom security attributes](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-update?view=graph-rest-1.0)
- [Conditional Access application assignments](https://learn.microsoft.com/en-us/graph/api/resources/conditionalaccessapplications?view=graph-rest-1.0)
- [Authentication session lifetime](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime)
