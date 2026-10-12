# Multi-Tenant BigQuery RLS Scaling Architecture: From `< 50` PoC to `10,000+` Enterprise Partners

## 1. Problem Statement: Why `1 Partner = 1 Service Account + 1 Row Access Policy` Breaks at Scale

In our PoC (`v2.3.0-bq-native-rls`, [`sql/01_poc_partner_sa_row_access_policies.sql`](../sql/01_poc_partner_sa_row_access_policies.sql)), each external retail partner is mapped to a dedicated Google Cloud Service Account (`sa-partner-polk@...`) and a dedicated BigQuery `ROW ACCESS POLICY` (`rls_partner_polk_store_2633`).

While this pattern provides a simple physical IAM boundary for **1 to 50 wholesale partners**, it hits two **hard Google Cloud platform limits** when scaling to **1,000 – 10,000+ retail partners or store managers**:

1. **BigQuery Hard Limit — Maximum 100 Row Access Policies per Table**:
   - BigQuery enforces a hard quota of **100 `ROW ACCESS POLICY` rules per table**.
   - Attempting to create Policy `#101` fails immediately with a DDL quota error, and evaluating dozens of distinct policy predicates degrades query planning latency.
2. **Cloud IAM Quota — Service Accounts per Project**:
   - A GCP project defaults to **100 Service Accounts** (with a hard ceiling of **1,000 Service Accounts**). You cannot provision 10,000 Service Accounts for 10,000 partners.
3. **Operational DDL Contention**:
   - Running `CREATE OR REPLACE ROW ACCESS POLICY` whenever a partner joins or changes stores causes schema-lock contention on the fact table.

---

## 2. What Scales to 100,000+ Users Without Modification

The following layers in this blueprint **already scale horizontally to 100,000+ LINE users** without any architectural changes:

- **LINE Per-User Rich Menu State Machine (`1-Card` $\leftrightarrow$ `6-Card`)**: LINE Messaging API supports linking Rich Menu IDs to millions of distinct `line_user_id` values.
- **Cloud Firestore Identity & Entitlement Registry (`line_partner_registry/{line_user_id}`)**: Firestore point-reads by document ID (`line_user_id`) execute in `< 5ms` regardless of whether the collection has 100 or 10,000,000 documents.
- **Zero-Token LLM Context (`ces.googleapis.com`)**: Passing only `x-line-user-id` and isolating `session_id = f"line-{line_user_id}-{partner_id}"` scales linearly.

---

## 3. Production Scaling Patterns for the BigQuery RLS Layer (`1,000 – 1,000,000+` Partners)

![Enterprise Multi-Tenant RLS Scaling Dataflow](../assets/diagrams/rls_scaling_10k_dataflow.png)

![Multi-Tenant RLS Scaling Blueprint](../assets/diagrams/slide_11_bq_rls_scaling_10k.png)

### Approach 3 (Recommended Enterprise Standard): Workforce Identity Federation (WIF) + 1 Dynamic Lookup `ROW ACCESS POLICY`

Implemented in **[`sql/02_enterprise_wif_dynamic_rls_10k_users.sql`](../sql/02_enterprise_wif_dynamic_rls_10k_users.sql)**.

#### How It Works
1. **Zero Partner Service Accounts (`0 SAs`)**:
   - Instead of impersonating a GCP Service Account per partner, the Stateless STS Broker exchanges the authenticated Partner JWT (from Google Identity Platform / CIP or LINE OIDC) via **Google Cloud Workforce Identity Federation (`sts.googleapis.com/v1/token`)**.
   - When the resulting WIF Bearer token calls BigQuery / BQCA, BigQuery's built-in `SESSION_USER()` function evaluates to that partner's unique identity (e.g., `partner.polk@iowa-beverage.com` or `principal://iam.googleapis.com/locations/global/workforcePools/partner-wif-pool/subject/PARTNER-POLK-001`).
2. **Clustered Entitlements Mapping Table (`rls_partner_entitlements`)**:
   - Store all 10,000+ partner mappings (`partner_principal`, `partner_id`, `rls_mode`, `allowed_county`, `allowed_store`, `status`) in a BigQuery table clustered by `partner_principal, status, allowed_store`.
3. **Only 2 Row Access Policies on the Fact Table**:
   - **Policy 1 (`rls_internal_all_access`)**: Grants `FILTER USING (TRUE)` to internal corporate employees (`domain:corp.example.com`).
   - **Policy 2 (`rls_dynamic_partner_lookup`)**: Grants access to the entire WIF pool using a single `EXISTS` subquery against `rls_partner_entitlements`:
     ```sql
     CREATE OR REPLACE ROW ACCESS POLICY rls_dynamic_partner_lookup
     ON `your-bigquery-data-project-id.iowa_liquor_sales.sales`
     GRANT TO (
       "principalSet://iam.googleapis.com/locations/global/workforcePools/partner-wif-pool/*"
     )
     FILTER USING (
       EXISTS (
         SELECT 1
         FROM `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements` e
         WHERE LOWER(e.partner_principal) = LOWER(SESSION_USER())
           AND e.status = 'ACTIVE'
           AND (
             e.rls_mode = 'ALL'
             OR (e.rls_mode = 'COUNTY' AND LOWER(sales.county) = e.allowed_county)
             OR (e.rls_mode = 'STORE'  AND LOWER(sales.county) = e.allowed_county AND sales.store_number = e.allowed_store)
           )
       )
     );
     ```
4. **Zero-DDL Day-2 Operations**:
   - Onboarding Partner `#10,001` is a single `INSERT INTO rls_partner_entitlements` row operation.
   - Revoking a partner is a single `UPDATE rls_partner_entitlements SET status = 'SUSPENDED'` statement.

---

### Approach 4 (Alternative Without WIF): Single Shared Service Account + Authorized TVF / SQL AST Rewriter

Implemented in **[`sql/03_authorized_tvf_parameterized_rls.sql`](../sql/03_authorized_tvf_parameterized_rls.sql)**.

When external partners authenticate via a custom portal without Workforce Identity Federation, all BigQuery queries execute under **1 shared Service Account** (meaning `SESSION_USER()` is constant). To guarantee deterministic Store-level isolation without relying on LLM prompt instructions:

1. **Authorized Table-Valued Function (`sales_for_partner(p_partner_id STRING)`)**:
   - Create a BigQuery TVF in an authorized view/dataset that takes `p_partner_id` as a parameter and joins `sales` against `rls_partner_entitlements`.
2. **Deterministic SQL AST Rewriter in FastMCP (`sqlglot`)**:
   - Before executing any generated SQL, the FastMCP server parses the SQL into an Abstract Syntax Tree (AST) using `sqlglot`, replaces references to the raw `sales` table with `sales_for_partner(@verified_partner_id)` (where `@verified_partner_id` is fetched directly from Firestore `line_partner_registry/{line_user_id}`, never from the LLM), and executes the parameterized query.
