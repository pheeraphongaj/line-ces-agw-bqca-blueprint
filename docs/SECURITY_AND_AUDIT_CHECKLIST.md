# Security, Redaction & Pre-Production Audit Checklist (`v2.4.0-gcaf-geap-agw`)

## 1. Automated PII, Token & Infrastructure Redaction Audit

All source code, SQL scripts, deployment scripts, HTML templates, and presentation assets in this repository have been audited against the following categories:

| Category | Audited Patterns | Status |
| :--- | :--- | :--- |
| **OAuth 2.0 & API Tokens** | `ya29.*`, `ghp_*`, `github_pat_*`, `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` | **0 Hardcoded Secrets** (Injected exclusively via Secret Manager / `.env`) |
| **Zero `x-token` in CES & MCP Schema** | `x-goog-user-token`, `user_oauth_token`, `x-token` | **100% Eliminated** (`CES McpToolset` sends ONLY `x-line-user-id`; `GE Agent Gateway ext_proc` injects `Authorization: Bearer <sts_token>`) |
| **Internal GCP Project IDs & Numbers** | Internal demo project IDs, project numbers, and default compute service account prefixes | **100% Parameterized** (`GCP_PROJECT_ID`, `BQ_DATA_PROJECT_ID`, `GCP_PROJECT_NUMBER`) |
| **Live Cloud Run Endpoints** | Hashed `*.a.run.app` and `*.run.app` service URLs | **100% Parameterized** (Resolved dynamically via `gcloud run services describe --format=value(status.url)`) |
| **Personal Identifiable Information (PII)** | Internal employee emails, LDAPs, real LINE Channel IDs, Bot handles, and LINE `userId` values (`U...`) | **100% Redacted** (Uses synthetic test identities in `.env.example` and mock partner catalog) |

---

## 2. GCAF + GEAP + GE Agent Gateway Pre-Production Security Checklist

- [ ] **Configure Production OAuth 2.0 / OIDC Identity Provider (`GCIP / Firebase Auth` or `Auth0 / Okta`)**:
  - Enable **Google Cloud Identity Platform (`GCIP` / Firebase Authentication, `identitytoolkit.googleapis.com`)** with **LINE Login OIDC Federation (`https://access.line.me`)** and a `beforeSignIn` Blocking Function that injects `{partner_id, store_number}` custom claims, or federate **Auth0 / Okta / Entra ID** into **GCP Workforce Identity Federation (`WIF`)** (`sts.googleapis.com/v1/token`).
  - Disable `ALLOW_MOCK_PARTNER_PORTAL_FALLBACK=false` in production so `/auth/partner-login` strictly verifies cryptographic RS256/ES256 OIDC ID tokens.
- [ ] **Vault 3LO Refresh Tokens in GE Agent Identity Auth Manager (Zero `gs://` Storage)**:
  - Store any 3-Legged OAuth (3LO) refresh tokens for third-party APIs in **GE Agent Identity Auth Manager** encrypted with **Cloud KMS CMEK**. Keep Firestore (`line_partner_registry/{uid}`) restricted to non-secret entitlement bindings (`oidc_sub`, `partner_id`, `status: ACTIVE | SUSPENDED`).
- [ ] **Verify Zero `x-token` in CES `App.variableDeclarations` and `McpToolset.customHeaders`**:
  - Confirm `App.variableDeclarations` contains only `line_user_id` and `line_reply_token`, `McpToolset.customHeaders` maps only `{"x-line-user-id": "$context.variables.line_user_id"}`, and `MCP_TOOL_SCHEMA` exposes only `question`.
- [ ] **Enforce GE Agent Gateway (`agent-gateway-in`) `REQUEST_AUTHZ`, `CONTENT_AUTHZ` & `ext_proc` Callout**:
  - Attach `REQUEST_AUTHZ` (`iap.googleapis.com`), `CONTENT_AUTHZ` (`modelarmor.<region>.rep.googleapis.com` for PIJB and SDP/DLP), and the `ext_proc` Service Extension (`/ext-proc/oauth-broker`) so `POST /mcp/` only accepts requests carrying AGW-injected `Authorization: Bearer <sts_token>` and `x-agw-verified-partner-id`.
- [ ] **Verify BigQuery `filteredDataViewer` IAM Separation & VPC Service Controls (`VPC-SC`)**:
  - Audit project and dataset IAM bindings on `$BQ_DATA_PROJECT_ID` to confirm no Partner Service Account or WIF principal holds `roles/bigquery.filteredDataViewer` at the project, dataset, or table level. Enclose Vertex AI, CES, Cloud Run, BQCA, and BigQuery inside a **VPC Service Controls** perimeter.
