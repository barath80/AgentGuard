"""Write the production Keycloak realm from the dev realm (deploy/keycloak/agentguard-realm.json).

    python3 deploy/vm/make_realm.py --console-origin https://agentguard.203-0-113-7.sslip.io > realm.json

Same roles, console client and claim mappers as dev, but no users (operators are added with
add-operator.sh, each with a temporary password), redirects only to the real console origin,
TLS required, a password policy and brute-force lockout. Agents are not in the realm file
either: add-agent.sh creates each one as its own confidential client.
"""

import argparse
import json
import sys
from pathlib import Path

DEV_REALM = Path(__file__).resolve().parents[1] / "keycloak" / "agentguard-realm.json"


def production_realm(console_origin: str) -> dict:
    realm = json.loads(DEV_REALM.read_text(encoding="utf-8"))
    origin = console_origin.rstrip("/")
    realm.pop("users", None)
    realm.update(
        sslRequired="external",
        registrationAllowed=False,
        resetPasswordAllowed=False,
        bruteForceProtected=True,
        failureFactor=5,
        permanentLockout=False,
        passwordPolicy="length(12) and notUsername and notEmail and passwordHistory(3)",
    )
    for client in realm["clients"]:
        if client["clientId"] == "agentguard-console":
            client["redirectUris"] = [f"{origin}/*"]
            client["webOrigins"] = [origin]
            client.setdefault("attributes", {})["post.logout.redirect.uris"] = f"{origin}/*"
    return realm


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--console-origin", required=True, help="e.g. https://agentguard.example.com")
    args = parser.parse_args()
    if not args.console_origin.startswith("https://"):
        parser.error("the console origin must be https://")
    json.dump(production_realm(args.console_origin), sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
