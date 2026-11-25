-- Narrow read-only permissions required by the C# dashboard summaries.
-- Apply only after candidate migration 0023 and the core/upload/definition grants.
-- This script never creates a role or applies schema changes.
BEGIN;

DO $$
DECLARE
    runtime_oid OID;
    required RECORD;
    relation_name TEXT;
BEGIN
    SELECT oid INTO runtime_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF runtime_oid IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE oid = runtime_oid
          AND rolcanlogin IS TRUE
          AND rolinherit IS FALSE
          AND rolsuper IS FALSE
          AND rolcreatedb IS FALSE
          AND rolcreaterole IS FALSE
          AND rolreplication IS FALSE
          AND rolbypassrls IS FALSE
    ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = runtime_oid) THEN
        RAISE EXCEPTION 'Runtime login is missing or not restricted; no changes applied';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(runtime_oid, 'public', 'USAGE')
       OR pg_catalog.has_schema_privilege(runtime_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Core schema permissions must be reviewed first; no changes applied';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relowner = runtime_oid
          AND c.relkind IN ('r','p','v','m','S','f')
          AND c.relname = ANY(ARRAY['verification_details','verification_sessions',
              'platform_tenant_holidays','client_template_upload_frequency','error_log'])
    ) THEN
        RAISE EXCEPTION 'Runtime login must not own dashboard source objects; no changes applied';
    END IF;

    FOR required IN SELECT * FROM (VALUES
        ('verification_details','id'),
        ('verification_details','session_id'),
        ('verification_details','client_data_id'),
        ('verification_details','transaction_ref'),
        ('verification_details','cleared_status'),
        ('verification_details','range_status'),
        ('verification_details','status'),
        ('verification_sessions','id'),
        ('verification_sessions','client_id'),
        ('verification_sessions','status'),
        ('platform_tenant_holidays','tenant_id'),
        ('platform_tenant_holidays','holiday_date'),
        ('platform_tenant_holidays','repeats_yearly'),
        ('error_log','tenant_id'),
        ('error_log','is_resolved')
    ) AS columns_required(table_name,column_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.table_name = required.table_name
              AND c.column_name = required.column_name
        ) THEN
            RAISE EXCEPTION 'Dashboard schema is incomplete at public.%; review migration 0023 and module schema before granting',
                required.table_name;
        END IF;
    END LOOP;

    IF to_regclass('public.client_template_upload_frequency') IS NOT NULL THEN
        FOR required IN SELECT * FROM (VALUES
            ('client_template_upload_frequency','client_id'),
            ('client_template_upload_frequency','template_id'),
            ('client_template_upload_frequency','frequency')
        ) AS columns_required(table_name,column_name)
        LOOP
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns c
                WHERE c.table_schema = 'public'
                  AND c.table_name = required.table_name
                  AND c.column_name = required.column_name
            ) THEN
                RAISE EXCEPTION 'Optional upload-frequency table has an unexpected layout; no changes applied';
            END IF;
        END LOOP;
    END IF;

    FOREACH relation_name IN ARRAY ARRAY['verification_details','verification_sessions',
        'platform_tenant_holidays','error_log']
    LOOP
        IF pg_catalog.has_table_privilege(runtime_oid, format('public.%I', relation_name), 'SELECT') THEN
            RAISE EXCEPTION 'Dashboard source public.% already has table-wide SELECT; review grants before applying', relation_name;
        END IF;
    END LOOP;

    IF to_regclass('public.client_template_upload_frequency') IS NOT NULL THEN
        IF pg_catalog.has_table_privilege(runtime_oid, 'public.client_template_upload_frequency', 'SELECT') THEN
            RAISE EXCEPTION 'Upload-frequency source already has table-wide SELECT; review grants before applying';
        END IF;
    END IF;
END $$;

GRANT SELECT (id,session_id,client_data_id,transaction_ref,cleared_status,range_status,status)
    ON TABLE public.verification_details TO audit_platform_runtime;
GRANT SELECT (id,client_id,status)
    ON TABLE public.verification_sessions TO audit_platform_runtime;
GRANT SELECT (tenant_id,holiday_date,repeats_yearly)
    ON TABLE public.platform_tenant_holidays TO audit_platform_runtime;
GRANT SELECT (tenant_id,is_resolved)
    ON TABLE public.error_log TO audit_platform_runtime;

DO $$
DECLARE
    runtime_oid OID;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    IF to_regclass('public.client_template_upload_frequency') IS NOT NULL THEN
        EXECUTE 'GRANT SELECT (client_id,template_id,frequency) ON TABLE public.client_template_upload_frequency TO audit_platform_runtime';
        IF NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','client_id','SELECT')
           OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','template_id','SELECT')
           OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','frequency','SELECT') THEN
            RAISE EXCEPTION 'Upload-frequency read grant verification failed; transaction rolled back';
        END IF;
    END IF;
END $$;

DO $$
DECLARE
    runtime_oid OID;
    required RECORD;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    FOR required IN SELECT * FROM (VALUES
        ('verification_details','id'),('verification_details','session_id'),
        ('verification_details','client_data_id'),('verification_details','transaction_ref'),
        ('verification_details','cleared_status'),('verification_details','range_status'),
        ('verification_details','status'),('verification_sessions','id'),
        ('verification_sessions','client_id'),('verification_sessions','status'),
        ('platform_tenant_holidays','tenant_id'),('platform_tenant_holidays','holiday_date'),
        ('platform_tenant_holidays','repeats_yearly'),('error_log','tenant_id'),('error_log','is_resolved')
    ) AS columns_required(table_name,column_name)
    LOOP
        IF NOT pg_catalog.has_column_privilege(runtime_oid,
            format('public.%I', required.table_name), required.column_name, 'SELECT') THEN
            RAISE EXCEPTION 'Dashboard read grant verification failed; transaction rolled back';
        END IF;
    END LOOP;
    IF to_regclass('public.client_template_upload_frequency') IS NOT NULL THEN
        IF NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','client_id','SELECT')
           OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','template_id','SELECT')
           OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_template_upload_frequency','frequency','SELECT') THEN
            RAISE EXCEPTION 'Upload-frequency read grant verification failed; transaction rolled back';
        END IF;
    END IF;
END $$;

COMMIT;
