"""Admin sign-in (device code) used by the setup scripts.

Usage: python setup/admin_login.py [--tenant <tenant-id-or-domain>]

Without --tenant it uses AZURE_TENANT_ID from .env; when that's empty, sign in with an admin account
of the tenant where the agent should be created. The tenant ID is saved to .env (AZURE_TENANT_ID).
"""
from __future__ import annotations

import argparse

from graph_admin import ADMIN_UPN, ENV_PATH, TENANT_ID, decode_jwt, login_device_code, read_env, update_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Admin sign-in for the setup scripts (device code).")
    parser.add_argument("--tenant", help="tenant ID or domain, e.g. contoso.onmicrosoft.com")
    args = parser.parse_args()

    tenant = args.tenant or TENANT_ID or "organizations"
    target = "any work or school account" if tenant == "organizations" else f"tenant {tenant}"
    print(f"Signing in ({target}) with the device code flow...", flush=True)
    result = login_device_code(tenant)
    claims = decode_jwt(result["access_token"])
    upn = claims.get("upn") or claims.get("unique_name")
    tenant_id = claims.get("tid")
    scopes = sorted(claims.get("scp", "").split())

    print(f"\nSigned in as: {upn} (tenant {tenant_id})")
    print("Granted Graph scopes:", ", ".join(scopes))
    if ADMIN_UPN and upn and upn.lower() != ADMIN_UPN.lower():
        print(f"WARNING: ADMIN_UPN is {ADMIN_UPN} but you signed in as {upn}.")
    if "Directory.AccessAsUser.All" in scopes:
        print("WARNING: the token contains Directory.AccessAsUser.All; Agent ID APIs reject such tokens (403).")

    previous = read_env(ENV_PATH).get("AZURE_TENANT_ID")
    if previous != tenant_id:
        if previous:
            print(f"NOTE: .env pointed to tenant {previous}; it now points to {tenant_id}. "
                  "Run setup/provision_entra.py to create the agent objects in this tenant.")
        update_env(ENV_PATH, {"AZURE_TENANT_ID": tenant_id})
        print("Saved AZURE_TENANT_ID to .env")


if __name__ == "__main__":
    main()
