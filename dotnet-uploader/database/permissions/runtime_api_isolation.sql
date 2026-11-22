-- Restrict the Supabase Data API roles on the C# application's public schema.
-- Apply only to the dedicated C# development project as postgres, after review.
-- This does not enable RLS or alter audit_platform_runtime's explicit grants.
-- Keep the Data API disabled: postgres cannot change supabase_admin's default
-- privileges, so Supabase-managed objects could otherwise gain API grants later.

BEGIN;

DO $guard$
DECLARE
    runtime_oid OID;
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Run this restricted access plan as the designated postgres owner';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
        RAISE EXCEPTION 'Expected Supabase API roles are missing';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname IN ('anon', 'authenticated')
          AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Supabase API roles have unexpected login or elevated attributes';
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
    ) THEN
        RAISE EXCEPTION 'The restricted C# runtime login is missing or has unsafe role attributes';
    END IF;

    IF EXISTS (
           SELECT 1
           FROM pg_catalog.pg_auth_members m
           WHERE m.member = runtime_oid
              OR (
                  m.roleid = runtime_oid
                  AND NOT (
                      m.member = 'postgres'::regrole
                      AND m.admin_option IS TRUE
                      AND m.inherit_option IS FALSE
                      AND m.set_option IS FALSE
                  )
              )
       )
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_class c
           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relowner = runtime_oid
             AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
       )
       OR pg_catalog.has_schema_privilege(runtime_oid, 'public', 'CREATE') THEN
        RAISE EXCEPTION 'The C# runtime login has an unapproved membership, owns a public object, or has public-schema CREATE access';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(runtime_oid, 'public', 'USAGE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid, 'public.users', 'username', 'SELECT')
       OR NOT pg_catalog.has_table_privilege(runtime_oid, 'public.sessions', 'DELETE') THEN
        RAISE EXCEPTION 'The restricted C# runtime core grants do not match the expected login/session contract';
    END IF;
END;
$guard$;

-- Remove existing direct Data API grants from application objects only.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM anon, authenticated, PUBLIC;

-- Prevent postgres-created public objects from restoring direct API-role grants.
-- PostgreSQL's global PUBLIC routine default remains; keep Data API disabled.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE EXECUTE ON ROUTINES FROM anon, authenticated;

DO $verify$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege('anon', c.oid, 'SELECT')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'INSERT')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'UPDATE')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'DELETE')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'TRUNCATE')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'REFERENCES')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'TRIGGER')
              OR pg_catalog.has_table_privilege('anon', c.oid, 'MAINTAIN')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'SELECT')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'INSERT')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'UPDATE')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'DELETE')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'TRUNCATE')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'REFERENCES')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'TRIGGER')
              OR pg_catalog.has_table_privilege('authenticated', c.oid, 'MAINTAIN')
          )
    ) THEN
        RAISE EXCEPTION 'A public API role retains access to a public-schema table';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege('anon', c.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege('anon', c.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege('anon', c.oid, 'UPDATE')
              OR pg_catalog.has_sequence_privilege('authenticated', c.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege('authenticated', c.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege('authenticated', c.oid, 'UPDATE')
          )
    ) THEN
        RAISE EXCEPTION 'A public API role retains access to a public-schema sequence';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND (
              pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE')
              OR pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE')
          )
    ) THEN
        RAISE EXCEPTION 'A public API role retains EXECUTE on a public-schema routine';
    END IF;
END;
$verify$;

COMMIT;
