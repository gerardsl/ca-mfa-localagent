# Walkthrough: every step and command

This is the full sequence used to build and verify the project, in order, including the problems hit
along the way and how they were fixed. All commands run in PowerShell 7 on Windows (Python 3.12) from
the project folder. Every script is idempotent and safe to re-run. Object names below are the defaults;
change them in `.env` (`BLUEPRINT_NAME`, `AGENT_IDENTITY_NAME`, `CLIENT_APP_NAME`, `CA_POLICY_NAME`).

## 1. Install dependencies and create `.env`

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env   # set MISTRAL_API_KEY; leave the Entra values empty
```

## 2. Admin sign-in with the device code flow

```powershell
python setup/admin_login.py            # or: --tenant <tenant-id-or-domain>
```

- Signs in your admin account with the Microsoft Graph Command Line Tools public client
  (`14d82eec-204b-4c2f-b7e8-296a70dab67e`, the client `Connect-MgGraph` uses). Open
  https://login.microsoft.com/device, enter the code, sign in and tick
  **Consent on behalf of your organization**.
- Requests every delegated Graph scope needed later in one go: `AgentIdentityBlueprint.*`,
  `AgentIdentityBlueprintPrincipal.Create`, `AgentIdentity.*`, `Application.ReadWrite.All`,
  `DelegatedPermissionGrant.ReadWrite.All`, `Policy.Read.All`, `Policy.ReadWrite.ConditionalAccess`,
  `AuditLog.Read.All`, `Directory.Read.All`, `User.Read`.
- Checks that the token doesn't carry `Directory.AccessAsUser.All`, which the Agent ID APIs reject (403).
- Saves the tenant ID to `.env` (`AZURE_TENANT_ID`). The token cache lives in
  `%LOCALAPPDATA%\ca-mfa-localagent`, outside the project; delete it when done (step 13).

## 3. Check the tenant prerequisites

```powershell
python setup/check_prereqs.py
```

Shows the admin's directory roles, whether security defaults are off (required for Conditional Access),
licenses with Entra ID P1/P2 or Agent 365 plans, and the existing Conditional Access policies. If the
tenant already has an MFA policy for all users and all apps, users already satisfy MFA when they use the
agent; the agent's own policy still guarantees it.

## 4. Provision the Agent ID objects

```powershell
python setup/provision_entra.py
```

Microsoft Graph calls, in order:

1. `POST /applications/microsoft.graph.agentIdentityBlueprint`: blueprint **Spain-News-Agent-Blueprint**,
   with the signed-in admin as sponsor and owner.
2. `POST /servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal`: the blueprint principal
   (not created automatically).
3. `PATCH /applications/{blueprint}`: identifier URI `api://{appId}`, delegated scope `access_as_user`,
   `requestedAccessTokenVersion: 2`. This is what makes it an OBO agent: users get tokens for the
   agent's API.
4. `POST /applications` and `POST /servicePrincipals`: public client **Spain-News-Agent-Client**
   (redirect URI `http://localhost`, public client flows on). Blueprints can't run interactive
   sign-ins, so users sign in through this client.
5. `PATCH /applications/{blueprint}` (`preAuthorizedApplications`) and `POST /oauth2PermissionGrants`
   (client to blueprint, `access_as_user`, all users): no consent prompt for users.
6. `POST /servicePrincipals/microsoft.graph.agentIdentity`: agent identity
   **Spain-News-Agent-Identity** under the blueprint.
7. `POST /oauth2PermissionGrants`: agent identity to Microsoft Graph, delegated `User.Read`, all users.
8. `POST /applications/{blueprint}/addPassword`: a 180-day client secret for local development.
   It's written to `.env` together with the tenant ID, app IDs and scope.

Problem hit: the first run stopped at step 5 because
`GET /oauth2PermissionGrants?$filter=clientId eq … and resourceId eq … and consentType eq …` returned
404 right after the service principal was created. Fix: filter on `clientId` only, match the rest in
code, and retry on replication delays. Re-running the script picked up where it stopped.

## 5. Verify the objects and the first token exchange step

```powershell
python setup/verify_entra.py
```

Checks the blueprint (identifier URI, scope, pre-authorized client), the agent identity (type, parent
blueprint, `User.Read` grant) and gets the blueprint's exchange token for the agent identity
(client credentials + `fmi_path`). The token's audience is `fb60f99c-7a34-4190-8149-302f77469936`
(AAD Token Exchange Endpoint) and its subject is the FMI path ending in the agent identity's appId.

## 6. Build and test the news agent (no sign-in)

```powershell
python tests/test_news_agent.py --runs 3
```

Problems seen with the small `ministral-3b-2512` model, and the fixes now in `agent.py`:

- Mistranslations and invented details: the prompt now requires facts from the tool results only.
- Links attached to the wrong article: each pick quotes the original title, and the title wins over
  a mismatched article id.
- The same event picked twice: a duplicate-event check sends targeted feedback to the model.
- Dozens of parallel searches in one turn (364 articles, 159 s): budget of 3 rounds and 6 tool calls.
- A 120 s read timeout from the Mistral API: `max_tokens` caps plus retries on timeouts.
- The English `geo/Spain` Google News section feed was empty, so English coverage uses a search
  (`Spain when:1d`).

## 7. End-to-end run before Conditional Access

```powershell
python agent.py --device-code --verbose
```

A user signs in with the printed link and code (if nobody signs in within 15 minutes, the code expires
and the CLI says so). Expected token claims:

- User token: `aud` = blueprint appId, `scp` = `access_as_user`.
- OBO Graph token: `appid` = the agent identity, `upn` = the user, `amr` includes `mfa` once MFA is
  required.
- `GET /me` works, and the agent prints the top 5 news about Spain.

## 8. Create the Conditional Access policy

```powershell
python setup/create_ca_policy.py
python setup/check_ca.py
```

- **Spain-News-Agent-CA**: all users, target resource **Spain-News-Agent-Blueprint**, grant: require
  MFA, sign-in frequency **Every time**. For OBO agents the user is the token subject, so the policy
  targets users and the agent's resource
  ([docs](https://learn.microsoft.com/entra/identity/conditional-access/agent-id)).
- Problem hit: the policy was created, but the immediate read-back returned 404 (replication). Fix:
  retry the read-back. Check the policy list for duplicates before re-running after such an error.
- `check_ca.py` runs the What If evaluation (`POST /identity/conditionalAccess/evaluate`), which shows
  the policy applies when a user signs in to the agent.

## 9. End-to-end run with the policy enforced

```powershell
python agent.py --device-code --verbose
python setup/check_ca.py
```

The run succeeds with `amr: pwd, mfa`. Sign-in logs can lag an hour; afterwards `check_ca.py` shows the
agent sign-in with `<policy name> -> success ['Mfa']`. The `50199` entries before each success are the
normal device code confirmation step.

## 10. Sign-in frequency "Every time"

`create_ca_policy.py` adds the session control sign-in frequency **Every time**, so a recent MFA in the
browser can't be reused. Entra rejects MFA-only re-authentication for it (`1144: The 'every time'
sign-in frequency session control only allows 'primaryAndSecondaryAuthentication'`), so each sign-in
asks for password + MFA (at most once every 5 minutes). `--no-reauth` removes the setting.

To see the prompt, wait at least 5 minutes after the last MFA, then run:

```powershell
python agent.py               # browser window
python agent.py --device-code # link + code
```

## 11. Web UI

```powershell
python tests/test_web_app.py      # 24 checks
python web_app.py                 # http://localhost:5050
```

- Sign-in uses the auth code flow with PKCE through the client app and its `http://localhost` redirect
  URI, so no extra app registration change is needed.
- Uses `response_mode=form_post` (MSAL warns about query responses), so codes never appear in URLs.
  The callback is a cross-site POST without the SameSite=Lax cookie, so pending sign-ins are looked up
  by their single-use `state`.
- Tokens and the Mistral key stay on the server. Every agent action starts with the OBO exchange as the
  agent identity.

## 12. Publish to GitHub

```powershell
git init -b main
git config user.name <github-user>
git config user.email <id>+<github-user>@users.noreply.github.com   # GitHub noreply address
git add -A   # .env is excluded by .gitignore; scan the staged files for secrets before committing
git commit -m "<message>"
gh repo create <owner>/<repo> --private --source . --remote origin --push
```

If `gh repo create` fails (it returned HTTP 500 for a few minutes once), create the repository with the
REST API and push with git:

```powershell
gh api -X POST user/repos -f name=<repo> -F private=true
git config --local credential.helper ""
git config --local --add credential.helper '!gh auth git-credential'
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

Gotcha: `POST /user/repos` ignores the `visibility` field; use `private=true`, or the repository is
created public.

## 13. Clean up

```powershell
Remove-Item "$env:LOCALAPPDATA\ca-mfa-localagent" -Recurse -Force   # cached admin tokens
```

## Microsoft Learn references

- [Create an agent identity blueprint](https://learn.microsoft.com/entra/agent-id/create-blueprint)
- [Agent OAuth flows: on-behalf-of](https://learn.microsoft.com/entra/agent-id/agent-on-behalf-of-oauth-flow)
- [Configure inheritable permissions for blueprints](https://learn.microsoft.com/entra/agent-id/configure-inheritable-permissions-blueprints)
- [Conditional Access for agents](https://learn.microsoft.com/entra/identity/conditional-access/agent-id)
- [Target agent identities in Conditional Access](https://learn.microsoft.com/entra/identity/conditional-access/howto-target-agent-identities)
- [Adaptive session lifetime (sign-in frequency)](https://learn.microsoft.com/entra/identity/conditional-access/concept-session-lifetime)
- [conditionalAccessRoot: evaluate (What If)](https://learn.microsoft.com/graph/api/conditionalaccessroot-evaluate)
