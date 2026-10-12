-- =============================================================================
-- File: sql/01_poc_partner_sa_row_access_policies.sql
-- Pattern: Approach 1 (< 50 Partners PoC / VIP Wholesale Tenants)
-- Description:
--   Enforces physical Row-Level Security (RLS) in the BigQuery Storage Engine
--   using Per-Partner Service Accounts + Native ROW ACCESS POLICY rules.
--
-- CRITICAL IAM GUARDRAIL (DO NOT VIOLATE):
--   1. Grant ONLY `roles/bigquery.dataViewer` on the Dataset/Table to Partner
--      Service Accounts (`sa-partner-polk@...`, `sa-partner-east@...`).
--   2. NEVER grant `roles/bigquery.filteredDataViewer` at the Project, Dataset,
--      or Table level! In BigQuery IAM, `CREATE ROW ACCESS POLICY ... GRANT TO`
--      binds `roles/bigquery.filteredDataViewer` on that specific policy.
--      Granting `filteredDataViewer` at the Project/Dataset level causes IAM
--      inheritance onto ALL policies (including `rls_internal_all_access`
--      with `FILTER USING (TRUE)`), which completely bypasses RLS!
-- =============================================================================

-- 1. Gold Partner Policy: Restricted strictly to Polk County, Store #2633 (HY-VEE #3)
CREATE OR REPLACE ROW ACCESS POLICY rls_partner_polk_store_2633
ON `your-bigquery-data-project-id.iowa_liquor_sales.sales`
GRANT TO (
  "serviceAccount:sa-partner-polk@your-agent-host-project-id.iam.gserviceaccount.com"
)
FILTER USING (
  LOWER(county) = 'polk' AND store_number = '2633'
);

-- 2. Silver Partner Policy: Restricted strictly to Linn County, Store #3773 (BENZ DISTRIBUTING)
CREATE OR REPLACE ROW ACCESS POLICY rls_partner_east_store_3773
ON `your-bigquery-data-project-id.iowa_liquor_sales.sales`
GRANT TO (
  "serviceAccount:sa-partner-east@your-agent-host-project-id.iam.gserviceaccount.com"
)
FILTER USING (
  LOWER(county) = 'linn' AND store_number = '3773'
);

-- 3. Internal Corporate SSO (Cloud IAP) & Platinum VIP Policy: Full Statewide Access (All 99 Counties / 2,940 Stores)
CREATE OR REPLACE ROW ACCESS POLICY rls_internal_all_access
ON `your-bigquery-data-project-id.iowa_liquor_sales.sales`
GRANT TO (
  "serviceAccount:123456789012-compute@developer.gserviceaccount.com",
  "user:analyst@corp.example.com"
)
FILTER USING (TRUE);
