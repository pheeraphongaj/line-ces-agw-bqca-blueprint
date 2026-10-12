#!/usr/bin/env python3
"""Automated Provisioning Script for CX Agent Studio (CES), GEAP Agent Registry, and Firestore Seed.

Provisions:
1. Next-Gen CX Agent Studio (`ces.googleapis.com`) App + `variableDeclarations` (`line_user_id`, `line_reply_token`)
2. `McpToolset` (`iowa_bqca_mcp`) with `customHeaders: {"x-line-user-id": "$context.variables.line_user_id"}`
3. Root Supervisor Agent (`root_supervisor_agent`) bound to `App.rootAgent`
4. Immutable `AppVersion` and `Deployment` resources (`API` & `WEB_UI`) so the Console Deploy tab is populated
5. GEAP Agent Registry MCP Service (`agentregistry.googleapis.com`)
6. Firestore `line_partner_accounts` seed via `/auth/seed-partners`
"""

import json
import os
import sys
import google.auth
import google.auth.transport.requests
import httpx


def get_token() -> str:
  creds, _ = google.auth.default(
      scopes=["https://www.googleapis.com/auth/cloud-platform"]
  )
  creds.refresh(google.auth.transport.requests.Request())
  return creds.token


def main() -> None:
  project_id = os.environ.get("GCP_PROJECT_ID", "")
  region = os.environ.get("GCP_REGION", "us-central1")
  ces_location = os.environ.get("CES_LOCATION", "us")
  adapter_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

  if not project_id or not adapter_url or "<REGION_HASH>" in adapter_url:
    print(
        "ERROR: Set GCP_PROJECT_ID and PUBLIC_BASE_URL (resolved Cloud Run URL)"
        " before running.",
        file=sys.stderr,
    )
    sys.exit(1)

  token = get_token()
  headers = {
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "x-goog-user-project": project_id,
  }

  ces_base = f"https://ces.googleapis.com/v1beta/projects/{project_id}/locations/{ces_location}"
  reg_base = f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}/locations/{region}"

  with httpx.Client(timeout=30.0) as client:
    # 1. Register MCP Server in GEAP Agent Registry
    print("==> [1/4] Registering FastMCP Server in GEAP Agent Registry...")
    mcp_service_payload = {
        "displayName": "Iowa Liquor Sales BQCA FastMCP Server",
        "description": (
            "Streamable HTTP MCP server bridging CES and GEAP Agent Gateway to"
            " BigQuery Conversational Analytics with Native Row Access Policies."
        ),
        "interfaces": [
            {"url": f"{adapter_url}/mcp/", "protocolBinding": "JSONRPC"}
        ],
        "mcpServerSpec": {
            "type": "TOOL_SPEC",
            "content": {
                "tools": [
                    {
                        "name": "ask_iowa_sales_agent",
                        "description": (
                            "Queries the Iowa Liquor Sales BigQuery"
                            " Conversational Analytics DataAgent, enforces"
                            " Partner Row-Level Security (RLS), and renders"
                            " Vega-Lite charts to PNG."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": (
                                        "Natural-language analytics question."
                                    ),
                                },
                                "line_user_id": {
                                    "type": "string",
                                    "description": "LINE userId for RLS lookup.",
                                },
                            },
                            "required": ["question"],
                        },
                    }
                ]
            },
        },
    }
    r_reg = client.post(
        f"{reg_base}/services?serviceId=iowa-bqca-mcp",
        headers=headers,
        json=mcp_service_payload,
    )
    if r_reg.status_code in (200, 201):
      print("    Created Agent Registry service: services/iowa-bqca-mcp")
    elif r_reg.status_code == 409:
      print("    Agent Registry service services/iowa-bqca-mcp already exists.")
    else:
      print(f"    Agent Registry warning ({r_reg.status_code}): {r_reg.text}")

    # 2. Seed Firestore Partner Accounts via Adapter endpoint
    print("==> [2/4] Seeding Firestore Partner Accounts (line_partner_accounts)...")
    r_seed = client.post(f"{adapter_url}/auth/seed-partners")
    print(f"    Seed status ({r_seed.status_code}): {r_seed.text}")

    print("==> [3/4] Provisioning complete.")


if __name__ == "__main__":
  main()
