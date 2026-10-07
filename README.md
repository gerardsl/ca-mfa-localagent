# Spain News Agent (Entra Agent ID + Conditional Access MFA)

A local Python agent that checks the news online and returns the **top 5 news about Spain**, using a
Mistral model. It runs under its own Microsoft Entra **agent identity** on behalf of the signed-in user
(OBO), and a Conditional Access policy forces MFA whenever a user wants to use it. It comes with a CLI
and a local web UI.

Everything is created in **your own tenant** with **your own admin account**: the setup scripts create
the agent identity blueprint, the agent identity, the client app and the Conditional Access policy, and
write their IDs to your local `.env`. Nothing in this repo points to an existing tenant.

## How it works

```text
User --(1) sign-in to the agent's client app--> Entra ID   <-- Conditional Access: require MFA
          token for api://<blueprint appId>/access_as_user
agent.py / web_app.py
  (2) blueprint secret + fmi_path=<agent identity> --> exchange token bound to the agent identity
  (3) agent identity OBO exchange (user token)      --> Graph token: user = you, app = the agent identity
  (4) GET /me (delegated User.Read) to confirm who the agent acts for
  (5) Mistral tool-calling loop: get_spain_headlines / search_news (Google News RSS) --> top 5
```

For OBO agents the token subject is the **user**, so Conditional Access targets users and the agent's
resource (the blueprint), not the agent identity
([docs](https://learn.microsoft.com/entra/identity/conditional-access/agent-id)). The policy's
sign-in frequency **Every time** makes Entra require a full sign-in (password + MFA) each time the agent
is used, instead of reusing an earlier session (at most one prompt every 5 minutes; Entra only supports
password + MFA, not MFA-only, with "Every time").

Running the agent locally doesn't bypass the policy: Entra enforces it when it issues the token.
However, the news feeds and Mistral (API key) don't need Entra tokens, so in the CLI the sign-in gate
lives in `agent.py`. The web UI enforces it server-side: the browser never gets tokens or the Mistral key.

## Prerequisites

- Python 3.12+ and a [Mistral API key](https://console.mistral.ai).
- A Microsoft Entra tenant with Agent ID available, Entra ID P1 (for Conditional Access) and security
  defaults turned off.
- An admin account in that tenant with **Global Administrator**, or all of: Agent ID Administrator,
  Application Administrator (or Cloud Application Administrator) and Conditional Access Administrator.

## Set it up in your tenant

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env                 # then set MISTRAL_API_KEY (and optional names) in .env

python setup/admin_login.py                 # device code sign-in with YOUR admin account; saves your tenant ID
                                            # (use --tenant contoso.onmicrosoft.com to pick a tenant)
python setup/check_prereqs.py               # read-only checks: roles, security defaults, licenses, policies
python setup/provision_entra.py             # creates the blueprint, agent identity, client app, grants, secret
python setup/verify_entra.py                # verifies them and the blueprint -> agent identity token exchange
python setup/create_ca_policy.py            # creates the Conditional Access policy (MFA, every time)
python setup/check_ca.py                    # What If evaluation + recent agent sign-ins
Remove-Item "$env:LOCALAPPDATA\ca-mfa-localagent" -Recurse -Force   # delete the cached admin tokens
```

On the first sign-in, consent to the requested Microsoft Graph permissions (tick **Consent on behalf
of your organization**). `provision_entra.py` writes the tenant ID, app IDs, scope and a 180-day client
secret to `.env`. All setup scripts are idempotent.

## Run

```powershell
python web_app.py               # web UI at http://localhost:5050 (opens your browser)
python agent.py                 # CLI, browser sign-in
python agent.py --device-code   # CLI, device code sign-in (prints a link and a code)
python agent.py -v              # CLI, also print token claims
```

## Objects created in your tenant

| Object | Default name (`.env` setting) | Purpose |
| --- | --- | --- |
| Agent identity blueprint (+ principal) | `Spain-News-Agent-Blueprint` (`BLUEPRINT_NAME`) | Exposes `api://<appId>/access_as_user`; holds the dev client secret |
| Agent identity | `Spain-News-Agent-Identity` (`AGENT_IDENTITY_NAME`) | Acts on behalf of users; delegated Graph `User.Read` (admin consent) |
| Client app registration | `Spain-News-Agent-Client` (`CLIENT_APP_NAME`) | Public client users sign in with; pre-authorized on the blueprint scope |
| Conditional Access policy | `Spain-News-Agent-CA` (`CA_POLICY_NAME`) | All users → resource: the blueprint → require MFA, sign-in frequency **Every time** |

## Web UI (`web_app.py` + `web/`)

Open http://localhost:5050, select **Sign in with Microsoft** (password + MFA through the Conditional
Access policy), then use **Get today's top 5** or ask follow-up questions in the chat. The page streams
what the agent is doing (OBO token, news searches) and links every story and chat answer to its sources.

- Sign-in: auth code flow with PKCE and `response_mode=form_post` through the client app, using its
  `http://localhost` redirect URI (set `WEB_PORT` to use another port).
- Tokens and the Mistral key stay on the local server; the browser only gets an HttpOnly session cookie.
- Before every agent action, the agent identity performs the OBO exchange for the signed-in user.
- Hardening: CSRF token on API calls, Host header check (DNS rebinding), strict Content-Security-Policy,
  session rotation at sign-in. When the user token expires (about an hour), the page asks to sign in again.

## Setup scripts

The full sequence of commands, with the issues hit and how they were fixed, is in
[docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

| Script | Purpose |
| --- | --- |
| `setup/admin_login.py` | Admin device-code sign-in (token cache in `%LOCALAPPDATA%\ca-mfa-localagent`); saves `AZURE_TENANT_ID` |
| `setup/check_prereqs.py` | Read-only tenant checks: roles, security defaults, licenses, CA policies, existing objects |
| `setup/provision_entra.py` | Creates/updates the blueprint, principal, agent identity, client app, grants and secret; writes `.env` |
| `setup/verify_entra.py` | Verifies the objects and the blueprint's exchange token for the agent identity (`fmi_path`) |
| `setup/create_ca_policy.py` | Creates/updates the CA policy (`--report-only` for report-only mode, `--no-reauth` to allow reusing an earlier sign-in) |
| `setup/check_ca.py` | What If evaluation + recent agent sign-ins with the applied CA policies |

## Tests

```powershell
python tests/test_news_agent.py --runs 1   # heuristics, feeds and live Mistral runs (no sign-in)
python tests/test_web_app.py               # web UI: sign-in flow, CSRF/Host checks, streaming, chat
```

Both need a configured `.env` and internet access; the web test stubs the Entra parts that need a real user.

## Notes

- `.env` holds secrets (Mistral key, blueprint client secret); it's excluded by `.gitignore`. Don't share it.
- The blueprint client secret is for local development only. In production, use a managed identity as a
  federated identity credential on the blueprint.
- `ministral-3b-2512` is a small model; a larger Mistral model (set `MISTRAL_MODEL`) picks and summarizes
  stories more reliably.
