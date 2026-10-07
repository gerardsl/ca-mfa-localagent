"""Microsoft Entra Agent ID authentication for the agent (on-behalf-of flow).

1. The user signs in to the agent's front end (the public client app created by
   setup/provision_entra.py) and gets a token for the agent's API:
   api://<blueprint appId>/access_as_user. Conditional Access is evaluated at this point and
   requires MFA (policy created by setup/create_ca_policy.py).
2. The agent identity blueprint authenticates with its credential and requests an exchange token
   bound to the agent identity (fmi_path = agent identity appId).
3. The agent identity exchanges the user's token (OBO) for a Microsoft Graph token, so it acts on
   behalf of the user under its own identity.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import msal
import requests

LOGIN = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SETTINGS = [
    "AZURE_TENANT_ID",
    "AGENT_CLIENT_APP_ID",
    "AGENT_SCOPE",
    "BLUEPRINT_APP_ID",
    "BLUEPRINT_CLIENT_SECRET",
    "AGENT_IDENTITY_APP_ID",
]


class AuthError(RuntimeError):
    def __init__(self, message: str, response: dict | None = None):
        super().__init__(message)
        self.response = response or {}

    @property
    def claims_challenge(self) -> str | None:
        return self.response.get("claims")


def decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@dataclass
class AgentSession:
    user_token: str
    graph_token: str

    @property
    def user_claims(self) -> dict:
        return decode_jwt(self.user_token)

    @property
    def graph_claims(self) -> dict:
        return decode_jwt(self.graph_token)

    def get_me(self) -> dict:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName,mail",
            headers={"Authorization": f"Bearer {self.graph_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


class AgentIdentityAuth:
    def __init__(
        self,
        tenant_id: str,
        client_app_id: str,
        agent_scope: str,
        blueprint_app_id: str,
        blueprint_secret: str,
        agent_identity_app_id: str,
    ):
        self.tenant_id = tenant_id
        self.client_app_id = client_app_id
        self.agent_scope = agent_scope
        self.blueprint_app_id = blueprint_app_id
        self.blueprint_secret = blueprint_secret
        self.agent_identity_app_id = agent_identity_app_id
        self._token_url = f"{LOGIN}/{tenant_id}/oauth2/v2.0/token"

    @classmethod
    def from_env(cls) -> "AgentIdentityAuth":
        missing = [name for name in SETTINGS if not os.environ.get(name) or os.environ[name].startswith("<")]
        if missing:
            raise AuthError(
                f"Missing settings in .env: {', '.join(missing)}. "
                "Run setup/admin_login.py and setup/provision_entra.py to create the agent in your tenant."
            )
        return cls(*(os.environ[name] for name in SETTINGS))

    def sign_in_user(
        self, device_code: bool = False, login_hint: str | None = None, claims_challenge: str | None = None
    ) -> str:
        """Interactive user sign-in for the agent's API. This is where MFA is enforced."""
        app = msal.PublicClientApplication(self.client_app_id, authority=f"{LOGIN}/{self.tenant_id}")
        scopes = [self.agent_scope]
        if device_code:
            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise AuthError(f"Could not start the device code sign-in: {flow.get('error_description')}", flow)
            print(flow["message"], flush=True)
            result = app.acquire_token_by_device_flow(flow, claims_challenge=claims_challenge)
            if result.get("error") in ("authorization_pending", "expired_token"):
                raise AuthError("The device code expired before the sign-in was completed. Please run the agent again.", result)
        else:
            print("A browser window will open for sign-in. If it doesn't appear, run: python agent.py --device-code", flush=True)
            result = app.acquire_token_interactive(
                scopes, login_hint=login_hint, claims_challenge=claims_challenge
            )
        if "access_token" not in result:
            raise AuthError(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}", result)
        return result["access_token"]

    def _token_request(self, data: dict) -> str:
        resp = requests.post(self._token_url, data=data, timeout=30)
        body = resp.json() if resp.content else {}
        if not resp.ok or "access_token" not in body:
            description = (body.get("error_description") or f"HTTP {resp.status_code}").splitlines()[0]
            raise AuthError(f"{body.get('error', 'token_error')}: {description}", body)
        return body["access_token"]

    def get_agent_exchange_token(self) -> str:
        """Blueprint credential + fmi_path -> exchange token bound to the agent identity."""
        return self._token_request(
            {
                "grant_type": "client_credentials",
                "client_id": self.blueprint_app_id,
                "client_secret": self.blueprint_secret,
                "scope": "api://AzureADTokenExchange/.default",
                "fmi_path": self.agent_identity_app_id,
            }
        )

    def exchange_on_behalf_of(self, user_token: str, scope: str = GRAPH_SCOPE) -> str:
        """The agent identity exchanges the user's token for a downstream token (OBO)."""
        return self._token_request(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": self.agent_identity_app_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self.get_agent_exchange_token(),
                "assertion": user_token,
                "requested_token_use": "on_behalf_of",
                "scope": scope,
            }
        )

    def authenticate(self, device_code: bool = False, login_hint: str | None = None) -> AgentSession:
        user_token = self.sign_in_user(device_code, login_hint)
        try:
            graph_token = self.exchange_on_behalf_of(user_token)
        except AuthError as e:
            if not e.claims_challenge:
                raise
            # Conditional Access on the downstream resource requires more (e.g. MFA): sign in again.
            user_token = self.sign_in_user(device_code, login_hint, claims_challenge=e.claims_challenge)
            graph_token = self.exchange_on_behalf_of(user_token)
        return AgentSession(user_token, graph_token)
