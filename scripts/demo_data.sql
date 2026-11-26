-- Test data for the analytics agent: an online clothes shop.
-- Copies Google's public TheLook eCommerce data into the dataset demo_shop, adds two helper
-- tables and writes descriptions that the agent reads.
--
-- Run it in the BigQuery console, or: bq query --use_legacy_sql=false < scripts/demo_data.sql
-- Safe to run again: every table is replaced. It reads about 0.5 GB (inside BigQuery's free
-- monthly amount) and stores about the same.
--
-- What each table is good for when you test the agent:
--   filters and GROUP BY  orders, order_items, products, users
--   joins                 order_items -> products -> distribution_centers, users -> country_regions
--   window functions      events (sessions, funnels), order_items (running totals, rankings)
--   charts                monthly revenue, revenue by category or region, funnel steps, plan vs actual

CREATE SCHEMA IF NOT EXISTS demo_shop OPTIONS (
  location = "US",
  description = "Test data for the analytics agent: an online clothes shop (Google public TheLook eCommerce data, plus two helper tables). Some rows have created_at a few days in the future, because the source data is generated ahead of time."
);

-- 1. Warehouses (10 rows).
CREATE OR REPLACE TABLE demo_shop.distribution_centers
OPTIONS (description = "Warehouses that ship the products. One row per warehouse, with its latitude and longitude.")
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.distribution_centers`;

-- 2. Product catalog.
CREATE OR REPLACE TABLE demo_shop.products
OPTIONS (description = "Product catalog. One row per product: name, brand, category, department (Men or Women), cost (what the shop pays) and retail_price (list price). distribution_center_id links to distribution_centers.id.")
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`;

-- 3. Customers. Email and street address are left out: the agent does not need them.
CREATE OR REPLACE TABLE demo_shop.users
OPTIONS (description = "Customers. One row per registered user: name, age, gender, city, state, country, location and traffic_source (how they found the shop). Country names are not cleaned (for example both Germany and Deutschland): join country_regions on users.country = country_regions.country_raw for clean names and regions.")
AS SELECT * EXCEPT (email, street_address) FROM `bigquery-public-data.thelook_ecommerce.users`;

-- 4. Orders: one row per order.
CREATE OR REPLACE TABLE demo_shop.orders
OPTIONS (description = "Orders. One row per order. status is Processing, Shipped, Complete, Cancelled or Returned. shipped_at, delivered_at and returned_at are empty until that step happens. num_of_item is the number of items. user_id links to users.id.")
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`;

-- 5. Order items: one row per item sold. Partitioned by month, so date filters read less data.
CREATE OR REPLACE TABLE demo_shop.order_items
PARTITION BY TIMESTAMP_TRUNC(created_at, MONTH)
CLUSTER BY status, product_id
OPTIONS (description = "Sales. One row per item in an order. sale_price is what the customer paid for the item. Revenue = SUM(sale_price) for items whose status is not Cancelled or Returned. Links: order_id to orders.order_id, product_id to products.id, user_id to users.id, inventory_item_id to inventory_items.id. Partitioned by month of created_at.")
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.order_items`;

-- 6. Stock: one row per physical item the shop bought.
CREATE OR REPLACE TABLE demo_shop.inventory_items
OPTIONS (description = "Stock. One row per physical item the shop bought. cost is what the shop paid. sold_at is empty while the item is still in stock. product_id links to products.id.")
AS SELECT * FROM `bigquery-public-data.thelook_ecommerce.inventory_items`;

-- 7. Website clicks (about 2.4 million rows). IP addresses are left out.
CREATE OR REPLACE TABLE demo_shop.events
PARTITION BY TIMESTAMP_TRUNC(created_at, MONTH)
CLUSTER BY event_type, session_id
OPTIONS (description = "Website clickstream. One row per page view or action. A visit is one session_id, and sequence_number orders the steps in a session (1, 2, 3 ...). event_type is home, department, product, cart, purchase or cancel. The shopping funnel is home, department, product, cart, purchase. user_id is empty for visitors who are not signed in. Partitioned by month of created_at.")
AS SELECT * EXCEPT (ip_address) FROM `bigquery-public-data.thelook_ecommerce.events`;

-- 8. Helper: clean country names and world regions. The raw names come from users.country.
CREATE OR REPLACE TABLE demo_shop.country_regions
OPTIONS (description = "Lookup table that cleans users.country. Join on users.country = country_regions.country_raw. country is the standard English name, iso_code the ISO 3166 two-letter code and region the world region. Germany and Deutschland both map to Germany.")
AS
SELECT * FROM UNNEST([
  STRUCT("China" AS country_raw, "China" AS country, "CN" AS iso_code, "Asia" AS region),
  ("United States", "United States", "US", "North America"),
  ("Brasil", "Brazil", "BR", "South America"),
  ("South Korea", "South Korea", "KR", "Asia"),
  ("United Kingdom", "United Kingdom", "GB", "Europe"),
  ("France", "France", "FR", "Europe"),
  ("Germany", "Germany", "DE", "Europe"),
  ("Deutschland", "Germany", "DE", "Europe"),
  ("Spain", "Spain", "ES", "Europe"),
  ("Japan", "Japan", "JP", "Asia"),
  ("Australia", "Australia", "AU", "Oceania"),
  ("Belgium", "Belgium", "BE", "Europe"),
  ("Poland", "Poland", "PL", "Europe"),
  ("Colombia", "Colombia", "CO", "South America"),
  ("Austria", "Austria", "AT", "Europe")
]);

-- 9. Helper: monthly revenue targets per category, made from last year's sales.
-- The shop grows fast: the median growth on the same month last year is about 1.7x (checked in
-- November 2026). A target of 1.7x means about half the categories hit it and half miss it.
CREATE OR REPLACE TABLE demo_shop.category_monthly_targets
OPTIONS (description = "Monthly revenue target per product category. The plan is 70 percent growth: revenue_target is 170 percent of the revenue of that category in the same month one year earlier. month is the first day of the month. To compare with actual revenue, join on products.category and DATE_TRUNC(DATE(order_items.created_at), MONTH). Months after today have a target but no sales yet.")
AS
SELECT
  p.category,
  DATE_ADD(DATE_TRUNC(DATE(oi.created_at), MONTH), INTERVAL 1 YEAR) AS month,
  ROUND(SUM(oi.sale_price) * 1.7, 2) AS revenue_target
FROM demo_shop.order_items AS oi
JOIN demo_shop.products AS p ON p.id = oi.product_id
WHERE oi.status NOT IN ("Cancelled", "Returned")
GROUP BY p.category, month;

-- Column notes the agent reads. A few per table, to stay under BigQuery's limit on table updates.
ALTER TABLE demo_shop.order_items ALTER COLUMN sale_price SET OPTIONS (description = "Price the customer paid for this item. Sum it for revenue, skipping Cancelled and Returned items.");
ALTER TABLE demo_shop.order_items ALTER COLUMN status SET OPTIONS (description = "Processing, Shipped, Complete, Cancelled or Returned.");
ALTER TABLE demo_shop.events ALTER COLUMN event_type SET OPTIONS (description = "home, department, product, cart, purchase or cancel.");
ALTER TABLE demo_shop.events ALTER COLUMN sequence_number SET OPTIONS (description = "Step number inside the session. 1 is the first page of the visit.");
ALTER TABLE demo_shop.inventory_items ALTER COLUMN sold_at SET OPTIONS (description = "When the item was sold. Empty while it is still in stock.");
ALTER TABLE demo_shop.users ALTER COLUMN country SET OPTIONS (description = "Raw country name, not cleaned. Join country_regions.country_raw for the clean name and region.");
ALTER TABLE demo_shop.products ALTER COLUMN cost SET OPTIONS (description = "What the shop pays for one unit. Margin = retail_price - cost.");
