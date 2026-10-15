# Assign custom security attributes to agents using Microsoft Graph

Custom security attributes are assigned to **service principals** (`/servicePrincipals`), not
application objects (`/applications`). An **Agent Identity Blueprint Principal** and an **Agent
Identity** use the same Graph `PATCH` operation, with their respective service principal object `id`.

This page shows how to assign `Org = IT` under the attribute set `CompanyOrg` to both objects.
Use the IDs created in **your own tenant**; all IDs and access tokens below are placeholders.

## Required permissions and roles

For delegated administration, both of the following are required:

| Requirement | Value |
| --- | --- |
| Microsoft Graph scope for assigning attributes | `CustomSecAttributeAssignment.ReadWrite.All` |
| Signed-in user's Entra role | **Attribute Assignment Administrator**, scoped to the attribute set or tenant |

**Global Administrator alone is not enough.** Granting the Graph scope does not replace the user's
attribute assignment role, and assigning the role does not replace Graph permission consent.

The lookup requests also need service-principal read permissions, such as `Application.Read.All`.
Reading assigned values requires `CustomSecAttributeAssignment.Read.All` or the assignment
read/write scope above, together with the appropriate attribute assignment role.

The `CompanyOrg` attribute set and `Org` definition must already exist. For the main example, `Org`
must be a **String** with **Allow multiple values to be assigned** enabled. If the definition only
allows predefined values, add `IT` first. Creating or changing definitions requires **Attribute
Definition Administrator**; assigning a value does not create the definition.

The repository's optional setup sign-in requests the extra attribute permissions:

```powershell
python setup\admin_login.py --attributes
# To select a tenant explicitly:
# python setup\admin_login.py --attributes --tenant contoso.onmicrosoft.com
```

For the raw HTTP examples, obtain a Microsoft Graph access token with the necessary consent.
Send it as `Authorization: Bearer {graph-access-token}`. A token for the local agent API is not a
Microsoft Graph token. Do not save tokens in source code or commit your `.env`.

## 1. Look up each service principal object ID

Use the application/client IDs from your local `.env` to locate the correct objects.

### Agent Blueprint Principal

```http
GET https://graph.microsoft.com/v1.0/servicePrincipals(appId='{BLUEPRINT_APP_ID}')?$select=id,appId,displayName
Authorization: Bearer {graph-access-token}
OData-Version: 4.0
```

Record the returned **`id`** as `{blueprint-principal-object-id}`.

### Agent Identity

```http
GET https://graph.microsoft.com/v1.0/servicePrincipals(appId='{AGENT_IDENTITY_APP_ID}')?$select=id,appId,displayName
Authorization: Bearer {graph-access-token}
OData-Version: 4.0
```

Record the returned **`id`** as `{agent-identity-object-id}`.

**Use `id`, not `appId`, in `/servicePrincipals/{sp-object-id}`.** Also do not substitute the blueprint
application object's ID: the application registration and its service principal are separate objects.
Check each returned `displayName` before making a change.

## 2. Read existing assignments

Before updating each object, read its current attributes:

```http
GET https://graph.microsoft.com/v1.0/servicePrincipals/{sp-object-id}?$select=id,displayName,customSecurityAttributes
Authorization: Bearer {graph-access-token}
OData-Version: 4.0
```

If `Org` already contains other values, retain them when adding `IT`. A collection update sets the
supplied collection; it is not an append operation. For example, retain `Finance` with
`"Org": ["Finance", "IT"]` rather than replacing it with `["IT"]`.

If attributes are omitted or access is denied, check permissions and role scope rather than assuming
no assignments exist.

## 3. Assign `Org = IT` to each service principal

Send the following request **twice**, substituting each object's ID:

| Request | Replace `{sp-object-id}` with |
| --- | --- |
| Tag the blueprint principal | `{blueprint-principal-object-id}` |
| Tag the agent identity | `{agent-identity-object-id}` |

```http
PATCH https://graph.microsoft.com/v1.0/servicePrincipals/{sp-object-id}
Authorization: Bearer {graph-access-token}
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

This body assumes `Org` has no other assigned values. Merge any values read in the previous step
before sending it. A successful update normally returns **`204 No Content`**.

The assignments are independent: tagging the blueprint principal does not tag its child identities,
and tagging an agent identity does not tag its parent blueprint principal.

### If `Org` is single-valued

Use this body only when the existing definition is a single-valued String. Omit `Org@odata.type` and
send a scalar instead of an array:

```json
{
  "customSecurityAttributes": {
    "CompanyOrg": {
      "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
      "Org": "IT"
    }
  }
}
```

Changing the request body does not change the definition from single-valued to multi-valued, or vice
versa. The repository's attribute-policy setup script expects the multi-valued definition used in the
[policy guide](ATTRIBUTE_POLICIES.md).

## 4. Verify both assignments

Repeat the step 2 `GET` for **each** service principal. For the multi-valued example, the returned
`customSecurityAttributes` should include:

```json
{
  "CompanyOrg": {
    "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
    "Org@odata.type": "#Collection(String)",
    "Org": ["IT"]
  }
}
```

Other existing values may also be present. Allow for directory replication if an immediate read
still shows the previous value; do not repeatedly overwrite assignments without first reading them.

## Which object matters for Conditional Access?

For this application's on-behalf-of (OBO) sign-in, the user requests a token for the blueprint's API.
A resource attribute filter therefore needs the tag on the **blueprint principal**. A tag only on
the child agent identity does not make that inbound sign-in match the blueprint resource filter.

Tagging does not create or enable a Conditional Access policy. See
[the two attribute-based MFA targeting options](ATTRIBUTE_POLICIES.md) for the policy configuration
and verification steps. The helper's `--tag-blueprint` option tags **only the blueprint principal**;
use the API requests above when you also want to tag the agent identity.

## References

- [Get a service principal](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-get?view=graph-rest-1.0)
- [Update a service principal and custom-attribute permissions](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-update?view=graph-rest-1.0)
- [Assign custom security attributes to applications](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/custom-security-attributes-apps)
