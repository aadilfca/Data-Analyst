-- Read-only definition access for the C# runtime login. No password is set here.
BEGIN;

DO $$
DECLARE role_oid OID;
BEGIN
    SELECT oid INTO role_oid FROM pg_catalog.pg_roles WHERE rolname = 'audit_platform_runtime';
    IF role_oid IS NULL THEN
        RAISE EXCEPTION 'audit_platform_runtime is missing; no changes applied';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE oid = role_oid
                   AND rolcanlogin AND NOT rolinherit AND NOT rolsuper
                   AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls)
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = role_oid)
       OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Runtime login is not restricted as expected; no changes applied';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.platform_tenant_columns)
       OR NOT EXISTS (SELECT 1 FROM public.platform_tenant_templates)
       OR (SELECT count(*) FROM public.template_column_mappings WHERE tenant_id IS NOT NULL) <> 93 THEN
        RAISE EXCEPTION 'Workspace definitions are not fully adopted; no changes applied';
    END IF;
END $$;

GRANT SELECT (id, column_name, data_type, is_active) ON public.master_columns TO audit_platform_runtime;
GRANT SELECT (column_id, tenant_id, display_name, category, notes, binding_origin, definition_version)
    ON public.platform_tenant_columns TO audit_platform_runtime;
GRANT SELECT (id, template_name, created_by, created_date, is_system, has_subtotals_by_default,
              data_purpose, ledger_columns, rate_source, is_active)
    ON public.templates TO audit_platform_runtime;
GRANT SELECT (template_id, tenant_id, display_name, template_kind, binding_origin, definition_version)
    ON public.platform_tenant_templates TO audit_platform_runtime;
GRANT SELECT (template_id, column_id, tenant_id, storage_key, sort_order, reconciliation_role,
              compare_for_changes, comparison_review_required)
    ON public.template_column_mappings TO audit_platform_runtime;

DO $$
DECLARE role_oid OID;
BEGIN
    SELECT oid INTO role_oid FROM pg_catalog.pg_roles WHERE rolname = 'audit_platform_runtime';
    IF NOT pg_catalog.has_column_privilege(role_oid, 'public.master_columns', 'column_name', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.platform_tenant_columns', 'display_name', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.templates', 'template_name', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.platform_tenant_templates', 'template_kind', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(role_oid, 'public.template_column_mappings', 'storage_key', 'SELECT')
       OR pg_catalog.has_table_privilege(role_oid, 'public.platform_tenant_columns', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_table_privilege(role_oid, 'public.platform_tenant_templates', 'INSERT,UPDATE,DELETE')
       OR pg_catalog.has_table_privilege(role_oid, 'public.template_column_mappings', 'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'Read-only definition grant verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
