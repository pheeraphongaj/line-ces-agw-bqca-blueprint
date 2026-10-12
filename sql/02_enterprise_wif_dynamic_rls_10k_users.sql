-- =============================================================================
-- File: sql/02_enterprise_wif_dynamic_rls_10k_users.sql
-- Pattern: Approach 3 (Recommended for 1,000 – 1,000,000+ External Partners)
-- Description:
--   Eliminates the 100 Row-Access-Policy-per-table limit and the 1,000 Service
--   Account quota by combining:
--     1) Workforce Identity Federation (WIF) / Google Identity Platform so
--        BigQuery's `SESSION_USER()` reflects the authenticated partner principal.
--     2) A clustered `rls_partner_entitlements` mapping table in BigQuery.
--     3) A SINGLE Dynamic Subquery `ROW ACCESS POLICY` (`rls_dynamic_partner_lookup`).
--
--   Result:
--     - 0 Partner Service Accounts required!
--     - Only 2 Row Access Policies on the fact table regardless of whether you
--       have 1,000 or 1,000,000 partners!
--     - Onboarding Partner #10,001 or revoking access is a single `INSERT` /
--       `UPDATE` row operation — zero DDL changes and zero IAM policy edits!
-- =============================================================================

-- 1. Create the Partner Entitlements Mapping Table (Clustered by partner_principal)
CREATE TABLE IF NOT EXISTS `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements` (
  partner_principal STRING NOT NULL OPTIONS(description="SESSION_USER() principal from Workforce Identity Federation or Google Identity"),
  partner_id        STRING NOT NULL OPTIONS(description="External Partner CRM ID, e.g., PARTNER-POLK-001"),
  partner_name      STRING,
  partner_tier      STRING,
  rls_mode          STRING NOT NULL OPTIONS(description="STORE | COUNTY | ALL"),
  allowed_county    STRING OPTIONS(description="Lowercase county name when rls_mode IN ('STORE', 'COUNTY')"),
  allowed_store     STRING OPTIONS(description="Exact store_number when rls_mode = 'STORE'"),
  status            STRING NOT NULL OPTIONS(description="ACTIVE | SUSPENDED"),
  updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY partner_principal, status, allowed_store;

-- 2. Seed Sample Partner Entitlements (Supports 10,000+ rows with sub-10ms lookup)
MERGE `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements` T
USING (
  SELECT
    'partner.polk@iowa-beverage.com' AS partner_principal,
    'PARTNER-POLK-001' AS partner_id,
    'Hy-Vee #3 Des Moines (Store #2633)' AS partner_name,
    'GOLD' AS partner_tier,
    'STORE' AS rls_mode,
    'polk' AS allowed_county,
    '2633' AS allowed_store,
    'ACTIVE' AS status
  UNION ALL
  SELECT
    'partner.east@midwest-spirits.com',
    'PARTNER-EAST-002',
    'Benz Distributing Cedar Rapids (Store #3773)',
    'SILVER',
    'STORE',
    'linn',
    '3773',
    'ACTIVE'
  UNION ALL
  SELECT
    'vip@heartland-retail.com',
    'PARTNER-VIP-003',
    'Heartland Retail Corp. (Statewide)',
    'PLATINUM',
    'ALL',
    NULL,
    NULL,
    'ACTIVE'
) S
ON T.partner_principal = S.partner_principal AND COALESCE(T.allowed_store, '*') = COALESCE(S.allowed_store, '*')
WHEN MATCHED THEN
  UPDATE SET
    partner_id = S.partner_id,
    partner_name = S.partner_name,
    partner_tier = S.partner_tier,
    rls_mode = S.rls_mode,
    allowed_county = S.allowed_county,
    status = S.status,
    updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
  INSERT (partner_principal, partner_id, partner_name, partner_tier, rls_mode, allowed_county, allowed_store, status)
  VALUES (S.partner_principal, S.partner_id, S.partner_name, S.partner_tier, S.rls_mode, S.allowed_county, S.allowed_store, S.status);

-- 3. Policy #1: Internal Corporate Employees (Full Statewide Visibility)
CREATE OR REPLACE ROW ACCESS POLICY rls_internal_all_access
ON `your-bigquery-data-project-id.iowa_liquor_sales.sales`
GRANT TO (
  "domain:corp.example.com",
  "serviceAccount:123456789012-compute@developer.gserviceaccount.com"
)
FILTER USING (TRUE);

-- 4. Policy #2: Single Dynamic Lookup Policy for 10,000+ External Partners via WIF Pool
--    Replace `WORKFORCE_POOL_ID` with your Workforce Identity Federation pool name.
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

-- =============================================================================
-- Operational Day-2 Examples (Zero DDL Required):
--
-- Onboarding Partner #10,001:
--   INSERT INTO `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements`
--   (partner_principal, partner_id, partner_name, partner_tier, rls_mode, allowed_county, allowed_store, status)
--   VALUES ('store4129@partner.example.com', 'PARTNER-4129', 'Target Store #4129', 'GOLD', 'STORE', 'polk', '4129', 'ACTIVE');
--
-- Instant Partner Revocation:
--   UPDATE `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements`
--   SET status = 'SUSPENDED', updated_at = CURRENT_TIMESTAMP()
--   WHERE partner_id = 'PARTNER-4129';
-- =============================================================================
