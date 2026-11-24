-- Least-privilege access for C# Verification and Trial Balance account routing.
-- Apply as the database owner after migrations 0021 and 0027 and runtime-role setup.
BEGIN;

DO $$
DECLARE role_oid OID;
BEGIN
    SELECT oid INTO role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF role_oid IS NULL THEN
        RAISE EXCEPTION 'audit_platform_runtime is missing; no changes applied';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE oid = role_oid
          AND rolcanlogin IS TRUE
          AND rolsuper IS FALSE
          AND rolcreatedb IS FALSE
          AND rolcreaterole IS FALSE
          AND rolreplication IS FALSE
          AND rolbypassrls IS FALSE
          AND rolinherit IS FALSE
    ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = role_oid)
      OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Runtime login is not restricted as expected; no changes applied';
    END IF;

    IF to_regclass('public.account_control_assignments') IS NULL
       OR to_regclass('public.platform_audit_rule_state') IS NULL
       OR to_regclass('public.platform_audit_rule_snapshots') IS NULL
       OR to_regclass('public.platform_audit_rule_category_snapshots') IS NULL THEN
        RAISE EXCEPTION 'Required C# Verification tables are missing; no changes applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner = role_oid
          AND c.relname IN ('account_control_assignments', 'platform_audit_rule_state',
                            'platform_audit_rule_snapshots', 'platform_audit_rule_category_snapshots')
    ) THEN
        RAISE EXCEPTION 'Runtime login must not own Verification objects; no changes applied';
    END IF;
END $$;

GRANT SELECT (client_id, account_code, verification_workstream,
              financial_statement_category_id, note_category_id, updated_by, updated_at)
    ON TABLE public.account_control_assignments TO audit_platform_runtime;
GRANT INSERT (client_id, account_code, verification_workstream,
              financial_statement_category_id, note_category_id, updated_by)
    ON TABLE public.account_control_assignments TO audit_platform_runtime;
GRANT UPDATE (verification_workstream, financial_statement_category_id,
              note_category_id, updated_by, updated_at)
    ON TABLE public.account_control_assignments TO audit_platform_runtime;

GRANT SELECT (tenant_id, config_version)
    ON TABLE public.platform_audit_rule_state TO audit_platform_runtime;
GRANT SELECT (tenant_id, config_version, schema_version, evaluator_version,
              evaluation_fingerprint, revision, payload)
    ON TABLE public.platform_audit_rule_snapshots TO audit_platform_runtime;
GRANT SELECT (tenant_id, category_id, config_version, evaluation_fingerprint)
    ON TABLE public.platform_audit_rule_category_snapshots TO audit_platform_runtime;

DO $$
DECLARE role_oid OID;
BEGIN
    SELECT oid INTO role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF NOT pg_catalog.has_column_privilege(role_oid, 'public.account_control_assignments', 'verification_workstream', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.platform_audit_rule_snapshots', 'payload', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.platform_audit_rule_state', 'config_version', 'SELECT')
       OR pg_catalog.has_table_privilege(role_oid, 'public.account_control_assignments', 'DELETE')
       OR pg_catalog.has_table_privilege(role_oid, 'public.platform_audit_rule_state', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_table_privilege(role_oid, 'public.platform_audit_rule_snapshots', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_table_privilege(role_oid, 'public.platform_audit_rule_category_snapshots', 'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'Verification grant verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
