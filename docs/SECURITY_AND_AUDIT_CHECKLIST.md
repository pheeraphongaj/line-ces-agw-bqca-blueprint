# Security, Redaction & Pre-Production Audit Checklist

## 1. Automated PII, Token & Infrastructure Redaction Audit

All source code, SQL scripts, deployment scripts, HTML templates, and presentation assets in this repository have been audited against the following categories:

| Category | Audited Patterns | Status |
| :--- | :--- | :--- |
| **OAuth 2.0 & API Tokens** | `ya29.*`, `ghp_*`, `github_pat_*`, `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` | **0 Hardcoded Secrets** (Injected exclusively via Secret Manager / `.env`) |
| **Internal GCP Project IDs & Numbers** | Internal demo project IDs, project numbers, and default compute service account prefixes | **100% Parameterized** (`GCP_PROJECT_ID`, `BQ_DATA_PROJECT_ID`, `GCP_PROJECT_NUMBER`) |
| **Live Cloud Run Endpoints** | Hashed `*.a.run.app` and `*.run.app` service URLs | **100% Parameterized** (Resolved dynamically via `gcloud run services describe --format=value(status.url)`) |
| **Personal Identifiable Information (PII)** | Internal employee emails, LDAPs, real LINE Channel IDs, Bot handles, and LINE `userId` values (`U...`) | **100% Redacted** (Uses synthetic test identities in `.env.example` and mock partner catalog) |

---

## 2. Production Security Checklist Before Customer Launch

- [ ] **Replace Mockup Partner Authentication (`/auth/portal`) with Customer Identity Platform (CIP) or Corporate IdP OIDC**:
  - The included `MOCK_PARTNER_ACCOUNTS` dictionary in `services/line-bqca-adapter/main.py` is designed for rapid sandbox verification. In production, verify Firebase Auth / Google Cloud Identity Platform (CIP) ID tokens or OIDC JWTs before writing to `line_partner_registry/{uid}`.
- [ ] **Restrict Cloud Run Ingress on Internal Endpoints**:
  - Protect `/ext-proc/oauth-broker`, `/auth/seed-partners`, and `/mcp/` using Cloud Run IAM authentication or VPC Direct Egress / Private Service Connect (PSC) from GEAP Agent Gateway, while keeping `/line/webhook` and `/auth/portal` publicly reachable for LINE Platform and mobile browsers.
- [ ] **Verify BigQuery `filteredDataViewer` IAM Separation**:
  - Audit project and dataset IAM bindings on `$BQ_DATA_PROJECT_ID` to confirm no Partner Service Account or WIF principal holds `roles/bigquery.filteredDataViewer` at the project, dataset, or table level.
- [ ] **Enable Model Armor Floor & Template on GEAP Agent Gateway**:
  - Attach `CONTENT_AUTHZ` (`modelarmor.<region>.rep.googleapis.com`) on your `AgentGateway` resource to inspect both incoming LINE user prompts and outgoing BQCA responses for prompt injection and sensitive data leakage.
