-- =============================================================================
-- File: sql/03_authorized_tvf_parameterized_rls.sql
-- Pattern: Approach 4 (1,000 – 100,000+ Partners with Single Shared Service Account)
-- Description:
--   When your MCP server executes queries using a SINGLE shared Service Account
--   (without Workforce Identity Federation), `SESSION_USER()` is always that
--   single Service Account.
--
--   To enforce hard server-side Row-Level Security for 10,000+ partners without
--   relying on LLM prompt instructions:
--     1) Store partner entitlements in `rls_partner_entitlements`.
--     2) Create an Authorized Table-Valued Function (TVF) `sales_for_partner`
--        that accepts the verified `p_partner_id` injected by the FastMCP server
--        (resolved from Firestore `line_partner_registry/{line_user_id}`).
-- =============================================================================

CREATE OR REPLACE TABLE FUNCTION `your-bigquery-data-project-id.iowa_liquor_sales.sales_for_partner`(
  p_partner_id STRING
)
AS (
  SELECT
    s.invoice_and_item_number,
    s.date,
    s.store_number,
    s.store_name,
    s.address,
    s.city,
    s.zip_code,
    s.county_number,
    s.county,
    s.category,
    s.category_name,
    s.vendor_number,
    s.vendor_name,
    s.item_number,
    s.item_description,
    s.pack,
    s.bottle_volume_ml,
    s.state_bottle_cost,
    s.state_bottle_retail,
    s.bottles_sold,
    s.sale_dollars,
    s.volume_sold_liters,
    s.volume_sold_gallons
  FROM `your-bigquery-data-project-id.iowa_liquor_sales.sales` AS s
  INNER JOIN `your-bigquery-data-project-id.iowa_liquor_sales.rls_partner_entitlements` AS e
    ON e.partner_id = p_partner_id
   AND e.status = 'ACTIVE'
   AND (
     e.rls_mode = 'ALL'
     OR (e.rls_mode = 'COUNTY' AND LOWER(s.county) = e.allowed_county)
     OR (e.rls_mode = 'STORE'  AND LOWER(s.county) = e.allowed_county AND s.store_number = e.allowed_store)
   )
);

-- Example Secure Query Invoked by FastMCP Server:
-- SELECT store_number, store_name, SUM(sale_dollars) AS total_sales
-- FROM `your-bigquery-data-project-id.iowa_liquor_sales.sales_for_partner`(@verified_partner_id)
-- WHERE EXTRACT(YEAR FROM date) = 2023
-- GROUP BY 1, 2
-- ORDER BY total_sales DESC
-- LIMIT 5;
