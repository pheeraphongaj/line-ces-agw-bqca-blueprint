# Step-by-Step Customer Implementation Guide

This guide walks through deploying the **LINE Official Account $\rightarrow$ CX Agent Studio (CES) $\rightarrow$ GEAP Agent Gateway $\rightarrow$ BigQuery Conversational Analytics (BQCA)** architecture (`v2.3.0-bq-native-rls`) in your own Google Cloud environment.

---

## Phase 1: Prerequisites & Google Cloud APIs

Enable the required APIs in your Agent Host Project (`$GCP_PROJECT_ID`):

```bash
export GCP_PROJECT_ID="your-agent-host-project-id"
export GCP_PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)')"
export GCP_REGION="us-central1"
export BQ_DATA_PROJECT_ID="your-bigquery-data-project-id"

gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  iamcredentials.googleapis.com \
  iap.googleapis.com \
  ces.googleapis.com \
  geminidataanalytics.googleapis.com \
  agentregistry.googleapis.com \
  networkservices.googleapis.com \
  bigquery.googleapis.com \
  storage.googleapis.com \
  --project="${GCP_PROJECT_ID}"
```

### 1.1 Create Firestore Database & Chart Storage Bucket

```bash
# Create Native-mode Firestore database (stores Partner Registry — zero OAuth tokens!)
gcloud firestore databases create \
  --database="(default)" \
  --location="${GCP_REGION}" \
  --type=firestore-native \
  --project="${GCP_PROJECT_ID}"

# Create Cloud Storage bucket for rendered Vega-Lite PNG charts
export CHART_BUCKET="${GCP_PROJECT_ID}-line-charts"
gcloud storage buckets create "gs://${CHART_BUCKET}" \
  --location="${GCP_REGION}" \
  --uniform-bucket-level-access \
  --project="${GCP_PROJECT_ID}"
```

### 1.2 Store LINE Messaging API Credentials in Secret Manager

> **Trap Guardrail (`Trap #5`)**: Always use `printf "%s"` when uploading secrets so no trailing newline (`\n`) corrupts `HMAC-SHA256` signature verification.

```bash
printf "%s" "YOUR_32_CHAR_LINE_CHANNEL_SECRET" | \
  gcloud secrets create line-channel-secret --data-file=- --project="${GCP_PROJECT_ID}"

printf "%s" "YOUR_LINE_CHANNEL_ACCESS_TOKEN" | \
  gcloud secrets create line-channel-access-token --data-file=- --project="${GCP_PROJECT_ID}"
```

---

## Phase 2: BigQuery Native Row-Level Security (`ROW ACCESS POLICY`)

### 2.1 Choose Your Tenant Scale Pattern

- **For `< 50` Partners (PoC / Dedicated Wholesale Distributors)**:
  Run [`sql/01_poc_partner_sa_row_access_policies.sql`](../sql/01_poc_partner_sa_row_access_policies.sql) after creating the Partner Service Accounts in Phase 3.
- **For `1,000 – 1,000,000+` Partners (Enterprise Production)**:
  Run [`sql/02_enterprise_wif_dynamic_rls_10k_users.sql`](../sql/02_enterprise_wif_dynamic_rls_10k_users.sql) to create the clustered `rls_partner_entitlements` table and the single dynamic subquery `ROW ACCESS POLICY` (`rls_dynamic_partner_lookup`). See [`docs/RLS_SCALING_ARCHITECTURE.md`](RLS_SCALING_ARCHITECTURE.md) for full details.

> **CRITICAL IAM GUARDRAIL (`roles/bigquery.filteredDataViewer`)**:
> Never grant `roles/bigquery.filteredDataViewer` at the GCP Project, Dataset, or Table IAM level! In BigQuery, `CREATE ROW ACCESS POLICY ... GRANT TO (...)` works by binding `roles/bigquery.filteredDataViewer` onto that specific policy resource. If you grant `roles/bigquery.filteredDataViewer` at the project or dataset level, IAM inheritance grants the principal `bigquery.rowAccessPolicies.getFilteredData` on **every** policy on the table (including `rls_internal_all_access` where `FILTER USING (TRUE)`), completely bypassing Row-Level Security!
> - **Correct Setup**: Grant **only** `roles/bigquery.dataViewer` on the dataset/project, and let `CREATE ROW ACCESS POLICY ... GRANT TO (...)` manage policy-level permissions.

---

## Phase 3: Provision LINE Rich Menus (`1-Card Login` & `6-Card Analytics`)

Run the automated provisioning script to upload both PNG templates in `assets/richmenus/` and set the **1-Card Login Gate** as the default Rich Menu:

```bash
export LINE_CHANNEL_ACCESS_TOKEN="YOUR_LINE_CHANNEL_ACCESS_TOKEN"
python3 scripts/configure_line_richmenus.py
```

Copy the resulting `LOGIN_RICHMENU_ID` and `UNLOCKED_RICHMENU_ID` into your `.env` file.

---

## Phase 4: Deploy Cloud Run Services (`line-iap-auth` & `line-bqca-adapter`)

1. Copy `.env.example` to `.env` and fill in your project and agent identifiers.
2. Execute the deployment script:

```bash
./scripts/deploy_cloud_run.sh
```

What `scripts/deploy_cloud_run.sh` performs:
1. Creates Partner Service Accounts (`sa-partner-polk`, `sa-partner-east`) and grants `roles/iam.serviceAccountTokenCreator` to the Cloud Run Host Service Account so `get_stateless_sts_token()` can mint short-lived tokens in RAM.
2. Deploys `line-iap-auth` with `--iap --no-allow-unauthenticated` for internal corporate employees (`X-Goog-IAP-JWT-Assertion` verification).
3. Deploys `line-bqca-adapter` with `/line/webhook`, `/auth/portal` (External Partner CIP Login), `/ext-proc/oauth-broker` (Stateless IAM STS Broker), `/mcp/` (Streamable HTTP FastMCP Server), and `/charts/{id}.png`.
4. Dynamically resolves both Cloud Run URLs and publishes `config/runtime.json` to `gs://${CHART_BUCKET}/config/runtime.json`.

---

## Phase 5: Configure CX Agent Studio (CES) & GEAP Agent Gateway

### 5.1 Register FastMCP Server & Seed Firestore Partner Accounts

```bash
export PUBLIC_BASE_URL="$(gcloud run services describe line-bqca-adapter --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" --format='value(status.url)')"
python3 scripts/provision_ces_and_registry.py
```

### 5.2 Configure CES App Variable Declarations & McpToolset

In Next-Gen CX Agent Studio (`ces.googleapis.com`):
1. Declare `line_user_id` (`STRING`) and `line_reply_token` (`STRING`) in `App.variable_declarations`.
   - **Why (`Trap #3`)**: In `session_service.proto`, `text` and `variables` share a `oneof` inside `SessionInput`. Always send a 2-element `inputs` array `[{"variables": {"line_user_id": "..."}}, {"text": "..."}]`, and declare the variables on the `App` resource so CES forwards `customHeaders` to your `McpToolset`.
2. Configure the `McpToolset` (`iowa_bqca_mcp`) with:
   - `serverAddress`: `<PUBLIC_BASE_URL>/mcp/`
   - `customHeaders`: `{"x-line-user-id": "$context.variables.line_user_id"}`
   - **Zero-Token LLM Rule**: Never pass `ya29...` OAuth tokens in CES session variables. Only `x-line-user-id` is passed; the downstream Stateless STS Broker resolves Firestore entitlements and mints the RAM token outside the LLM context window.
3. Create an immutable `AppVersion` (`v1.0`) and `Deployment` resources (`API` and `WEB_UI`) so the Console UI's **Deploy / Channels** tab displays active channels (`Trap #4`).

---

## Phase 6: Configure LINE Developers Console Webhook

1. Open the [LINE Developers Console](https://developers.line.biz/console/) and select your Messaging API channel.
2. Set **Webhook URL** to:
   ```text
   https://<YOUR_LINE_BQCA_ADAPTER_URL>/line/webhook
   ```
3. Enable **Use webhook**.
4. In [LINE Official Account Manager](https://manager.line.biz/), set **Response mode** to **Bot** and disable **Auto-response messages**.
5. Ensure no Manager UI Rich Menu is active so the Messaging API Default Rich Menu (`1-Card Login Gate`) is shown to unauthenticated users.

---

## Phase 7: End-to-End Verification & Demo Accounts

Run the automated E2E verification script:

```bash
export PUBLIC_BASE_URL="$(gcloud run services describe line-bqca-adapter --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" --format='value(status.url)')"
python3 scripts/verify_e2e_rls.py
```

### Default Seeded Mockup Partner Accounts (For Testing `/auth/portal`)

| Partner Tier | Login Email | Password | Partner ID | Enforced BigQuery `ROW ACCESS POLICY` |
| :--- | :--- | :--- | :--- | :--- |
| **Gold Partner** | `partner.polk@iowa-beverage.com` | `IowaPartner#2026` | `PARTNER-POLK-001` | `rls_partner_polk_store_2633` (`LOWER(county) = 'polk' AND store_number = '2633'` — **1 Store Only**) |
| **Silver Partner** | `partner.east@midwest-spirits.com` | `Midwest#2026` | `PARTNER-EAST-002` | `rls_partner_east_store_3773` (`LOWER(county) = 'linn' AND store_number = '3773'` — **1 Store Only**) |
| **Platinum VIP** | `vip@heartland-retail.com` | `Heartland#2026` | `PARTNER-VIP-003` | `rls_internal_all_access` (`FILTER USING (TRUE)` — All 2,940 Stores) |
| **Suspended** | `suspended@old-liquor.com` | `Revoked#2026` | `PARTNER-REV-999` | Blocked at Pre-Check & AGW (`HTTP 403 Forbidden`); locks Rich Menu back to 1-Card Gate |
