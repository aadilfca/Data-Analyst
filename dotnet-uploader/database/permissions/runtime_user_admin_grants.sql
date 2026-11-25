-- Narrow runtime permissions for tenant-scoped User administration.
-- Apply only to the dedicated C# development database after migrations 0001-0007
-- and runtime_core_grants.sql have been reviewed and applied.
-- This script grants no global role, client-master, workspace, or transaction writes.

BEGIN;

DO $$
DECLARE
    runtime_oid OID;
    users_id_sequence REGCLASS;
BEGIN
    SELECT oid INTO runtime_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF runtime_oid IS NULL THEN
        RAISE EXCEPTION 'Create the restricted audit_platform_runtime login before applying user administration grants';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE oid = runtime_oid
          AND rolcanlogin IS TRUE
          AND rolsuper IS FALSE
          AND rolcreatedb IS FALSE
          AND rolcreaterole IS FALSE
          AND rolreplication IS FALSE
          AND rolbypassrls IS FALSE
          AND rolinherit IS FALSE
    ) THEN
        RAISE EXCEPTION 'Runtime login must be LOGIN, NOINHERIT, and have no elevated role attributes';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = runtime_oid) THEN
        RAISE EXCEPTION 'Runtime login must not inherit or assume another role';
    END IF;

    IF to_regclass('public.users') IS NULL
       OR to_regclass('public.platform_tenant_memberships') IS NULL
       OR to_regclass('public.platform_client_user_assignments') IS NULL
       OR to_regclass('public.sessions') IS NULL
       OR to_regclass('public.audit_log') IS NULL THEN
        RAISE EXCEPTION 'Required user administration tables are missing; apply reviewed foundation migrations first';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='platform_tenant_memberships'
          AND column_name='tenant_role'
    ) THEN
        RAISE EXCEPTION 'Tenant membership roles are missing; apply the reviewed tenant user roles migration first';
    END IF;

    users_id_sequence := pg_catalog.pg_get_serial_sequence('public.users', 'id')::REGCLASS;
    IF users_id_sequence IS NULL THEN
        RAISE EXCEPTION 'Expected identity sequence is missing for public.users';
    END IF;

    -- Refuse broad privileges that would make the column-level grant boundary
    -- ineffective. The core script's last_login column update remains allowed.
    IF pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'DELETE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'DELETE')
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='users' AND privilege_type='UPDATE'
             AND column_name NOT IN ('last_login','username','password_hash')
       )
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='users' AND privilege_type='INSERT'
             AND column_name NOT IN ('username','password_hash','is_active')
       )
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='platform_tenant_memberships' AND privilege_type='INSERT'
             AND column_name NOT IN ('tenant_id','user_id','tenant_role')
       )
       OR EXISTS (
           SELECT 1 FROM information_schema.column_privileges
           WHERE grantee='audit_platform_runtime' AND table_schema='public'
             AND table_name='platform_tenant_memberships' AND privilege_type='UPDATE'
             AND column_name NOT IN ('tenant_role','is_active')
       ) THEN
        RAISE EXCEPTION 'Unexpected broad user or membership permissions exist; review before applying';
    END IF;
END $$;

-- User identity fields needed by the tenant-user API. The global role column
-- is deliberately excluded; the API omits it so new users remain non-platform-admins.
GRANT INSERT (username,password_hash,is_active)
    ON TABLE public.users TO audit_platform_runtime;
GRANT UPDATE (username,password_hash)
    ON TABLE public.users TO audit_platform_runtime;

-- Tenant membership creation, role changes, and soft deactivation/reactivation.
GRANT INSERT (tenant_id,user_id,tenant_role)
    ON TABLE public.platform_tenant_memberships TO audit_platform_runtime;
GRANT UPDATE (tenant_role,is_active)
    ON TABLE public.platform_tenant_memberships TO audit_platform_runtime;

-- Client assignments are tenant constrained by composite foreign keys.
-- Users can only deactivate an assignment or change its team role; hard delete is not granted.
GRANT INSERT (tenant_id,client_id,user_id,team_role)
    ON TABLE public.platform_client_user_assignments TO audit_platform_runtime;
GRANT UPDATE (team_role,is_active)
    ON TABLE public.platform_client_user_assignments TO audit_platform_runtime;

-- The users identity value is generated by PostgreSQL for each new account.
DO $$
DECLARE
    users_id_sequence REGCLASS;
BEGIN
    users_id_sequence := pg_catalog.pg_get_serial_sequence('public.users', 'id')::REGCLASS;
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO audit_platform_runtime', users_id_sequence);
END $$;

DO $$
DECLARE runtime_oid OID;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    IF pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.users', 'DELETE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_tenant_memberships', 'DELETE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_client_user_assignments', 'INSERT')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_client_user_assignments', 'UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid, 'public.platform_client_user_assignments', 'DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_client_user_assignments' AND privilege_type='INSERT'
                    AND column_name NOT IN ('tenant_id','client_id','user_id','team_role'))
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_client_user_assignments' AND privilege_type='UPDATE'
                    AND column_name NOT IN ('team_role','is_active'))
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'tenant_id', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'client_id', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'user_id', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'team_role', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'team_role', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_client_user_assignments', 'is_active', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'username', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'password_hash', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'is_active', 'INSERT')
       OR pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'role', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'username', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'password_hash', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_memberships', 'tenant_role', 'INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_memberships', 'tenant_role', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.platform_tenant_memberships', 'is_active', 'UPDATE')
       OR NOT pg_catalog.has_sequence_privilege(runtime_oid, pg_catalog.pg_get_serial_sequence('public.users', 'id'), 'USAGE')
       OR NOT pg_catalog.has_table_privilege(runtime_oid, 'public.sessions', 'DELETE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.audit_log', 'action', 'INSERT') THEN
        RAISE EXCEPTION 'User administration permission verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
