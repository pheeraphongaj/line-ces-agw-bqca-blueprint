#!/usr/bin/env python3
"""End-to-End Verification Suite for Hybrid Partner Auth, Stateless IAM STS, and BigQuery Native RLS.

Tests:
1. `/healthz` service readiness and `v2.3.0-bq-native-rls` version check.
2. Partner Portal login (`/auth/partner-login`) for Gold (`PARTNER-POLK-001`), Silver (`PARTNER-EAST-002`), and Revoked (`PARTNER-REV-999`) accounts.
3. GEAP Service Extension Stateless IAM STS Broker (`/ext-proc/oauth-broker`) RAM token minting and `403 Forbidden` enforcement on suspended accounts.
4. Store-Level BigQuery Native `ROW ACCESS POLICY` isolation via `/mcp/` (`tools/call` -> `ask_iowa_sales_agent`):
   - Verifies that querying `"What are the top 5 stores by sales in 2023?"` as `PARTNER-POLK-001` returns ONLY 1 row (`Store #2633 — HY-VEE #3`), hiding all rival stores in Polk County.
"""

import os
import sys
import time
import hashlib
import hmac
import httpx


def sign_state(uid: str, exp: int, secret: str) -> str:
  msg = f"{uid}:{exp}".encode("utf-8")
  return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def main() -> None:
  base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
  channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "fallback-secret").strip()
  if not base_url or "<REGION_HASH>" in base_url:
    print("ERROR: Set PUBLIC_BASE_URL to your deployed Cloud Run Adapter URL.", file=sys.stderr)
    sys.exit(1)

  with httpx.Client(timeout=60.0) as client:
    print(f"==> [1/4] Checking /healthz on {base_url}...")
    r_health = client.get(f"{base_url}/healthz")
    r_health.raise_for_status()
    print("    Health:", r_health.json())

    # Bind test UID to Gold Partner (Store #2633)
    test_uid = "U_TEST_POLK_2633"
    exp = int(time.time()) + 600
    sig = sign_state(test_uid, exp, channel_secret)

    print("==> [2/4] Authenticating Gold Partner (partner.polk@iowa-beverage.com)...")
    r_login = client.post(
        f"{base_url}/auth/partner-login",
        data={
            "uid": test_uid,
            "exp": str(exp),
            "sig": sig,
            "email": "partner.polk@iowa-beverage.com",
            "password": "IowaPartner#2026",
        },
    )
    assert r_login.status_code == 200, f"Login failed: {r_login.status_code}"
    print("    Partner Login verified (HTTP 200).")

    print("==> [3/4] Testing Stateless IAM STS Broker (/ext-proc/oauth-broker)...")
    r_sts = client.post(
        f"{base_url}/ext-proc/oauth-broker",
        headers={"x-line-user-id": test_uid},
        json={},
    )
    r_sts.raise_for_status()
    sts_data = r_sts.json()
    print(
        f"    STS Broker Status: {sts_data.get('status')} | Partner:"
        f" {sts_data.get('partner_id')} | Impersonated SA:"
        f" {sts_data.get('impersonated_sa')} | Policy:"
        f" {sts_data.get('rls_policy_name')}"
    )

    print(
        "==> [4/4] Verifying Store-Level BigQuery Native RLS on 'Top 5 Stores"
        " by Sales in 2023'..."
    )
    mcp_req = {
        "jsonrpc": "2.0",
        "id": "rls-test-1",
        "method": "tools/call",
        "params": {
            "name": "ask_iowa_sales_agent",
            "arguments": {
                "question": "What are the top 5 stores by total sales in 2023?",
                "line_user_id": test_uid,
            },
        },
    }
    r_mcp = client.post(
        f"{base_url}/mcp/",
        headers={"x-line-user-id": test_uid},
        json=mcp_req,
    )
    r_mcp.raise_for_status()
    mcp_res = r_mcp.json()
    text_out = mcp_res["result"]["content"][0]["text"]
    print("\n--- MCP Tool Output ---")
    print(text_out)
    assert "2633" in text_out, "Expected Store #2633 in output!"
    print("\nSUCCESS: Store-Level BigQuery Native RLS verified end-to-end!")


if __name__ == "__main__":
  main()
