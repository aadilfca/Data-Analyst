-- Narrow calendar reads for the C# Data Bank runtime.
-- Apply only to the dedicated development project as the database owner.
-- This grants no writes and does not enable history, exports, or purge.
BEGIN;

DO $$
DECLARE
    runtime_oid OID;
    expected_columns TEXT[] := ARRAY['id','tenant_id','holiday_date','label','repeats_yearly'];
BEGIN
    IF current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'Unexpected database; no changes applied';
    END IF;

    SELECT oid INTO runtime_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF runtime_oid IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE oid = runtime_oid
          AND rolcanlogin IS TRUE AND rolinherit IS FALSE
          AND rolsuper IS FALSE AND rolcreatedb IS FALSE
          AND rolcreaterole IS FALSE AND rolreplication IS FALSE
          AND rolbypassrls IS FALSE
    ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = runtime_oid)
      OR pg_catalog.has_schema_privilege(runtime_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Runtime login is not restricted as expected; no changes applied';
    END IF;

    IF to_regclass('public.platform_tenant_holidays') IS NULL
       OR (SELECT count(*) FROM information_schema.columns
           WHERE table_schema='public' AND table_name='platform_tenant_holidays'
             AND column_name = ANY(expected_columns)) <> cardinality(expected_columns) THEN
        RAISE EXCEPTION 'Holiday table layout is missing or unexpected; no changes applied';
    END IF;

    IF pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_holidays', 'SELECT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_holidays', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_any_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'INSERT,UPDATE') THEN
        RAISE EXCEPTION 'Runtime login has unexpected broad holiday-table privileges; no changes applied';
    END IF;
END $$;

GRANT SELECT (id, tenant_id, holiday_date, label, repeats_yearly)
    ON TABLE public.platform_tenant_holidays TO audit_platform_runtime;

DO $$
DECLARE runtime_oid OID;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    IF NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'tenant_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'holiday_date', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'label', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'repeats_yearly', 'SELECT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_holidays', 'SELECT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_holidays', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_any_column_privilege(runtime_oid, 'public.platform_tenant_holidays', 'INSERT,UPDATE') THEN
        RAISE EXCEPTION 'Data Bank calendar grant verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
