-- Core runtime grants for login, tenant selection, user/client reads, and logs.
-- Execute only through a separately approved owner session after migrations 0001-0007.
-- Creates the runtime login without a password, then grants only core app access.
-- Set its password separately with psql's interactive `\password` command.
-- No module writes, transaction access, default privileges, or schema ownership are granted.

BEGIN;

-- No password is embedded in SQL or retained in query history.
CREATE ROLE audit_platform_runtime LOGIN NOINHERIT;

DO $$
DECLARE
    runtime_oid OID;
BEGIN
    SELECT oid INTO runtime_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'audit_platform_runtime';

    IF runtime_oid IS NULL THEN
        RAISE EXCEPTION 'Create the restricted audit_platform_runtime login before applying this grant plan';
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

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relowner = runtime_oid
          AND c.relkind IN ('r','p','v','m','S','f')
    ) THEN
        RAISE EXCEPTION 'Runtime login must not own application objects';
    END IF;

    IF pg_catalog.has_schema_privilege(runtime_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'Runtime login must not have CREATE on the public schema';
    END IF;
END $$;

GRANT CONNECT ON DATABASE postgres TO audit_platform_runtime;
GRANT USAGE ON SCHEMA public TO audit_platform_runtime;

-- Login/session authorization. UPDATE is limited to the fields required for
-- login bookkeeping and the existing session-selected workspace/client.
GRANT SELECT (id, username, password_hash, is_active, role, last_login)
    ON TABLE public.users TO audit_platform_runtime;
GRANT UPDATE (last_login) ON TABLE public.users TO audit_platform_runtime;

GRANT SELECT (token, user_id, expiry, selected_client_id, selected_tenant_id, context_version)
    ON TABLE public.sessions TO audit_platform_runtime;
GRANT INSERT (token, user_id, expiry) ON TABLE public.sessions TO audit_platform_runtime;
GRANT UPDATE (selected_client_id, selected_tenant_id, context_version)
    ON TABLE public.sessions TO audit_platform_runtime;
GRANT DELETE ON TABLE public.sessions TO audit_platform_runtime;

-- Workspace and client-card reads. No membership, client, or engagement writes.
GRANT SELECT (id, name, tenant_type, is_active)
    ON TABLE public.platform_tenants TO audit_platform_runtime;
GRANT SELECT (tenant_id, user_id, is_active, tenant_role)
    ON TABLE public.platform_tenant_memberships TO audit_platform_runtime;
GRANT SELECT (client_id, tenant_id)
    ON TABLE public.platform_tenant_client_homes TO audit_platform_runtime;
GRANT SELECT (id, client_code, client_name, industry, contact_person, email, phone, is_active, created_at)
    ON TABLE public.clients TO audit_platform_runtime;
GRANT SELECT (id, firm_tenant_id, client_id, is_active)
    ON TABLE public.platform_audit_engagements TO audit_platform_runtime;
GRANT SELECT (engagement_id, firm_tenant_id, user_id, is_active)
    ON TABLE public.platform_engagement_assignments TO audit_platform_runtime;

-- Audited core events and sanitized diagnostics. These grants do not expose log reads.
GRANT INSERT ("user", action, module, details, event_id, operation_id,
              actor_user_id, actor_kind, client_id, correlation_id),
     SELECT (id)
    ON TABLE public.audit_log TO audit_platform_runtime;
GRANT SELECT (id, code, user_message, suggested_action, is_active)
    ON TABLE public.error_codes TO audit_platform_runtime;
GRANT INSERT (error_code_id, error_code, user_id, client_id, module, endpoint,
              method, status_code, error_category, error_message, stack_trace,
              input_data, suggested_fix, event_id, operation_id, actor_user_id,
              actor_kind, correlation_id),
     SELECT (id)
    ON TABLE public.error_log TO audit_platform_runtime;

-- Only the two identity sequences used by the permitted log inserts.
DO $$
DECLARE
    table_name TEXT;
    sequence_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['audit_log', 'error_log'] LOOP
        sequence_name := pg_catalog.pg_get_serial_sequence(format('public.%I', table_name), 'id');
        IF sequence_name IS NULL THEN
            RAISE EXCEPTION 'Expected identity sequence is missing for public.%', table_name;
        END IF;
        EXECUTE format('GRANT USAGE ON SEQUENCE %s TO audit_platform_runtime', sequence_name::regclass);
    END LOOP;
END $$;

COMMIT;
