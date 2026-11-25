-- Narrow runtime permissions for tenant-admin client creation only.
-- Apply to the dedicated C# development database after migrations 0001-0007,
-- runtime_core_grants.sql, and runtime_user_admin_grants.sql are reviewed.
-- This does not grant client edits, client deactivation, hard deletes, or financial writes.

BEGIN;

DO $$
DECLARE
    runtime_oid OID;
    clients_id_sequence REGCLASS;
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
          AND rolcanlogin IS TRUE
          AND rolsuper IS FALSE
          AND rolcreatedb IS FALSE
          AND rolcreaterole IS FALSE
          AND rolreplication IS FALSE
          AND rolbypassrls IS FALSE
          AND rolinherit IS FALSE
    ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = runtime_oid)
      OR pg_catalog.has_schema_privilege(runtime_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Runtime login is missing or not restricted; no changes applied';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner = runtime_oid
          AND c.relkind IN ('r','p','v','m','S','f')
    ) THEN
        RAISE EXCEPTION 'Runtime login must not own application objects';
    END IF;

    IF to_regclass('public.clients') IS NULL
       OR to_regclass('public.platform_tenant_client_homes') IS NULL
       OR to_regclass('public.platform_tenants') IS NULL
       OR to_regclass('public.platform_tenant_memberships') IS NULL
       OR to_regclass('public.sessions') IS NULL
       OR to_regclass('public.users') IS NULL
       OR to_regclass('public.audit_log') IS NULL THEN
        RAISE EXCEPTION 'Required client creation tables are missing; apply reviewed foundations first';
    END IF;

    clients_id_sequence := pg_catalog.pg_get_serial_sequence('public.clients', 'id')::REGCLASS;
    IF clients_id_sequence IS NULL THEN
        RAISE EXCEPTION 'Expected identity sequence is missing for public.clients';
    END IF;

    -- Refuse broad or unexpected writes rather than silently accepting a
    -- permission plan that would permit client edits or cross-scope changes.
    IF pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'DELETE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'DELETE')
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='clients'
             AND ((privilege_type='INSERT' AND column_name NOT IN
                   ('client_code','client_name','industry','contact_person','email','phone'))
               OR privilege_type IN ('UPDATE','DELETE'))
       )
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='platform_tenant_client_homes'
             AND ((privilege_type='INSERT' AND column_name NOT IN ('client_id','tenant_id'))
               OR privilege_type IN ('UPDATE','DELETE'))
       ) THEN
        RAISE EXCEPTION 'Unexpected client or tenant-home write permissions exist; review before applying';
    END IF;
END $$;

-- Only fields used to create a client profile and its tenant home are writable.
GRANT INSERT (client_code,client_name,industry,contact_person,email,phone)
    ON TABLE public.clients TO audit_platform_runtime;
GRANT INSERT (client_id,tenant_id)
    ON TABLE public.platform_tenant_client_homes TO audit_platform_runtime;

DO $$
DECLARE clients_id_sequence REGCLASS;
BEGIN
    clients_id_sequence := pg_catalog.pg_get_serial_sequence('public.clients', 'id')::REGCLASS;
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO audit_platform_runtime', clients_id_sequence);
END $$;

DO $$
DECLARE
    runtime_oid OID;
    clients_id_sequence REGCLASS;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    clients_id_sequence := pg_catalog.pg_get_serial_sequence('public.clients', 'id')::REGCLASS;

    IF pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.clients', 'DELETE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'DELETE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'client_code', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'client_name', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'industry', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'contact_person', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'email', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.clients', 'phone', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'client_id', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_client_homes', 'tenant_id', 'INSERT')
       OR NOT pg_catalog.has_sequence_privilege(runtime_oid, clients_id_sequence, 'USAGE')
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='clients'
             AND ((privilege_type='INSERT' AND column_name NOT IN
                   ('client_code','client_name','industry','contact_person','email','phone'))
               OR privilege_type IN ('UPDATE','DELETE'))
       )
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='platform_tenant_client_homes'
             AND ((privilege_type='INSERT' AND column_name NOT IN ('client_id','tenant_id'))
               OR privilege_type IN ('UPDATE','DELETE'))
       ) THEN
        RAISE EXCEPTION 'Client creation permission verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
