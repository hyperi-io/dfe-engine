Restricted ClickHouse User with Parameterized Queries
ClickHouse doesn't have a native "stored procedure" or "allowed query whitelist" feature, but you can achieve this pattern using parameterized views combined with a tightly restricted role. Here's the approach:
1. Create the Role and User
sqlCREATE ROLE restricted_reader;
CREATE USER app_user IDENTIFIED BY 'secure_password' DEFAULT ROLE restricted_reader;
2. Create Parameterized Views as Your "Allowed Queries"
Parameterized views (available since ClickHouse 23.x) act as your predefined query whitelist. Each view encapsulates one allowed query:
sql-- Example: allowed query 1 - get orders by customer within a date range
CREATE VIEW allowed_query_orders AS
SELECT *
FROM orders
WHERE customer_id = {customer_id:UInt64}
  AND order_date BETWEEN {from_date:Date} AND {to_date:Date};

-- Example: allowed query 2 - aggregate stats by region
CREATE VIEW allowed_query_region_stats AS
SELECT region, count() AS cnt, sum(amount) AS total
FROM orders
WHERE region = {region:String}
GROUP BY region;
3. Grant Access Only to the Views
sql-- Grant SELECT only on the specific views, nothing else
GRANT SELECT ON default.allowed_query_orders TO restricted_reader;
GRANT SELECT ON default.allowed_query_region_stats TO restricted_reader;

-- Explicitly deny everything else (no access to underlying tables)
-- By default, having no grants means no access, but you can be explicit:
REVOKE ALL ON *.* FROM restricted_reader;
GRANT SELECT ON default.allowed_query_orders TO restricted_reader;
GRANT SELECT ON default.allowed_query_region_stats TO restricted_reader;
4. Query as the Restricted User
sql-- This works:
SELECT * FROM allowed_query_orders(customer_id=12345, from_date='2025-01-01', to_date='2025-01-31');

-- This is denied (no direct table access):
SELECT * FROM orders;  -- ACCESS_DENIED
5. Optional: Lock Down Further with Settings Profiles
sqlCREATE SETTINGS PROFILE restricted_profile
SETTINGS
    readonly = 1,                          -- no writes
    max_execution_time = 30,               -- query timeout
    max_rows_to_read = 1000000,            -- limit scan size
    allow_ddl = 0;                         -- no DDL

ALTER USER app_user SETTINGS PROFILE restricted_profile;
Key Points

Parameterized views are the whitelist — each one is a single allowed query shape. The user can only call these views, not the underlying tables.
RBAC enforces it — the role has SELECT only on specific views, so any other query fails with ACCESS_DENIED.
Parameters are type-safe — {param:Type} syntax prevents injection since ClickHouse enforces the declared type.
If you need write operations in the whitelist, you'd need to proxy those through an application layer or use ClickHouse's HTTP handler with predefined queries (<predefined_query_handler> in config.xml), which is another valid approach for a strict query whitelist over HTTP.

Alternative: Predefined HTTP Query Handlers
If your client connects over HTTP, ClickHouse natively supports a query whitelist in the server config:
xml<http_handlers>
    <rule>
        <url>/query/orders_by_customer</url>
        <methods>GET</methods>
        <handler>
            <type>predefined_query_handler</type>
            <query>
                SELECT * FROM orders
                WHERE customer_id = {customer_id:UInt64}
                  AND order_date BETWEEN {from_date:Date} AND {to_date:Date}
            </query>
        </handler>
    </rule>
</http_handlers>
This is the most locked-down option — the user never even sends SQL, just hits an endpoint with parameters.