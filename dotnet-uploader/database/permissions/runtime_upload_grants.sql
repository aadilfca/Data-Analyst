-- Restricted C# upload and mapping permissions, applied to the Supabase test
-- project after separate owner-session approval on 2026-11-21.
-- This file sets no password, creates no roles, and grants no Cashflows writes.

BEGIN;

DO $$
DECLARE
    runtime_oid OID;
    required_table TEXT;
    guard_table RECORD;
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
        RAISE EXCEPTION 'Runtime login is missing or not restricted; no changes applied';
    END IF;

    FOREACH required_table IN ARRAY ARRAY[
        'uploaded_files','upload_validation_plans','client_data','client_data_history',
        'data_audit_log','platform_upload_staging','platform_upload_validation_plans',
        'platform_upload_imports','platform_upload_import_receipts','platform_upload_jobs',
        'templates','platform_template_upload_bindings','platform_tenant_saved_mapping_sets',
        'platform_tenant_saved_mappings','platform_tenant_templates','platform_tenant_columns',
        'template_column_mappings','master_columns','sessions','users','platform_tenants',
        'platform_tenant_memberships','platform_tenant_client_homes','clients',
        'platform_audit_engagements','platform_engagement_assignments','platform_client_user_assignments'
    ] LOOP
        IF to_regclass(format('public.%I', required_table)) IS NULL THEN
            RAISE EXCEPTION 'A required C# upload table is missing; no changes applied';
        END IF;
    END LOOP;

    -- Evidence guards vary with legacy module availability. If present, each
    -- must expose the exact reference column used by the importer.
    FOR guard_table IN SELECT * FROM (VALUES
        ('verification_details','client_data_id'),
        ('voucher_line_documents','client_data_id'),
        ('voucher_line_physical_bill_items','source_document_row_id'),
        ('cashflow_prepared_lines','source_client_data_id')
    ) AS guard(table_name,column_name) LOOP
        IF to_regclass(format('public.%I', guard_table.table_name)) IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name=guard_table.table_name
                 AND column_name=guard_table.column_name
           ) THEN
            RAISE EXCEPTION 'An optional evidence table has an unexpected layout; no changes applied';
        END IF;
    END LOOP;
    IF to_regclass('public.verification_details') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='verification_details'
                         AND column_name='validation_status') THEN
        RAISE EXCEPTION 'Verification review status column is missing; no changes applied';
    END IF;
    IF to_regclass('public.platform_saved_mapping_adoption_review') IS NOT NULL
       AND (SELECT count(*) FROM information_schema.columns
            WHERE table_schema='public' AND table_name='platform_saved_mapping_adoption_review'
              AND column_name = ANY(ARRAY['tenant_id','client_id','template_id'])) <> 3 THEN
        RAISE EXCEPTION 'Saved-mapping review table has an unexpected layout; no changes applied';
    END IF;

    -- User administration is separately approved and uses only these narrow
    -- column grants. Permit them here so this script can be safely rerun after
    -- the user-admin grant plan; continue to reject table-wide or extra writes.
    IF (pg_catalog.has_any_column_privilege(runtime_oid,'public.clients','INSERT,UPDATE')
       OR pg_catalog.has_table_privilege(runtime_oid,'public.clients','INSERT,UPDATE,DELETE'))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.users','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public' AND table_name='users'
                    AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('username','password_hash','is_active'))
                          OR (privilege_type='UPDATE' AND column_name IN ('last_login','username','password_hash'))))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_memberships','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_tenant_memberships' AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('tenant_id','user_id','tenant_role'))
                          OR (privilege_type='UPDATE' AND column_name IN ('tenant_role','is_active'))))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_client_user_assignments','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_client_user_assignments' AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('tenant_id','client_id','user_id','team_role'))
                          OR (privilege_type='UPDATE' AND column_name IN ('team_role','is_active'))))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.platform_tenant_columns','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_columns','DELETE'))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.platform_tenant_templates','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_templates','DELETE'))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.template_column_mappings','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.template_column_mappings','DELETE')) THEN
        RAISE EXCEPTION 'Runtime login has unexpected setup or definition write rights; no changes applied';
    END IF;
END $$;

-- Temporary workbook receipts and plans. Staging rows are retained by their
-- database guard; normal cleanup expires the private file, not its receipt.
GRANT SELECT ON TABLE public.uploaded_files TO audit_platform_runtime;
GRANT INSERT (file_name,file_path,file_type,record_count,status,uploaded_by,client_id,template_id),
    UPDATE (record_count,status) ON TABLE public.uploaded_files TO audit_platform_runtime;
GRANT SELECT ON TABLE public.platform_upload_staging TO audit_platform_runtime;
GRANT INSERT (request_id,upload_id,tenant_id,user_id,client_id,template_id,storage_key,expires_at),
    UPDATE (state,byte_count,sha256,discovery) ON TABLE public.platform_upload_staging TO audit_platform_runtime;
GRANT SELECT, DELETE ON TABLE public.upload_validation_plans TO audit_platform_runtime;
GRANT INSERT (plan_id,file_id,client_id,template_id,file_sha256,request_signature,plan_json,status,created_at,expires_at,applied_at),
    UPDATE (status,plan_json,applied_at) ON TABLE public.upload_validation_plans TO audit_platform_runtime;
GRANT SELECT, INSERT, DELETE ON TABLE public.platform_upload_validation_plans TO audit_platform_runtime;
GRANT SELECT, INSERT ON TABLE public.platform_upload_imports TO audit_platform_runtime;
GRANT SELECT, INSERT ON TABLE public.platform_upload_import_receipts TO audit_platform_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.platform_upload_jobs TO audit_platform_runtime;

-- Mapping choices are scoped by the API to the selected workspace, client and
-- template. Definition tables themselves remain read-only for the runtime app.
GRANT SELECT ON TABLE public.platform_tenant_saved_mapping_sets,
    public.platform_tenant_saved_mappings TO audit_platform_runtime;
GRANT INSERT (tenant_id,client_id,template_id,template_binding_key),
    UPDATE (template_binding_key,definition_version)
    ON TABLE public.platform_tenant_saved_mapping_sets TO audit_platform_runtime;
GRANT INSERT, DELETE ON TABLE public.platform_tenant_saved_mappings TO audit_platform_runtime;
GRANT SELECT ON TABLE public.platform_template_upload_bindings TO audit_platform_runtime;
DO $$
BEGIN
    IF to_regclass('public.platform_saved_mapping_adoption_review') IS NOT NULL THEN
        GRANT SELECT (tenant_id,client_id,template_id)
            ON TABLE public.platform_saved_mapping_adoption_review TO audit_platform_runtime;
    END IF;
END $$;
GRANT UPDATE (status) ON TABLE public.upload_validation_plans TO audit_platform_runtime;

-- The canonical transaction table is the only current-data write target.
-- History stores prior versions; audit-log deletion is limited to the linked
-- client-data rows selected by the guarded import path.
GRANT SELECT, DELETE ON TABLE public.client_data TO audit_platform_runtime;
GRANT INSERT (client_id,template_id,upload_id,row_data,extract_date,created_by,is_deleted,version,
    document_group_id,change_detection_status,source_identity,source_fingerprint,source_row_number,source_order_upload_id)
    ON TABLE public.client_data TO audit_platform_runtime;
GRANT UPDATE (row_data,upload_id,extract_date,document_group_id,source_identity,source_fingerprint,
    source_row_number,source_order_upload_id,version,change_detection_status)
    ON TABLE public.client_data TO audit_platform_runtime;
GRANT SELECT, INSERT ON TABLE public.client_data_history TO audit_platform_runtime;
GRANT SELECT (client_data_id), DELETE ON TABLE public.data_audit_log TO audit_platform_runtime;

-- Changing transaction values invalidates prior review status. Optional legacy
-- evidence tables get only the reference-column read and MAINTAIN needed for a
-- transaction-scoped guard lock; they receive no row-write permissions here.
DO $$
DECLARE
    guard_table RECORD;
BEGIN
    FOR guard_table IN SELECT * FROM (VALUES
        ('verification_details','client_data_id'),
        ('voucher_line_documents','client_data_id'),
        ('voucher_line_physical_bill_items','source_document_row_id'),
        ('cashflow_prepared_lines','source_client_data_id')
    ) AS guard(table_name,column_name) LOOP
        IF to_regclass(format('public.%I', guard_table.table_name)) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT (%I) ON TABLE public.%I TO audit_platform_runtime',
                guard_table.column_name, guard_table.table_name);
            EXECUTE format('GRANT MAINTAIN ON TABLE public.%I TO audit_platform_runtime', guard_table.table_name);
            IF guard_table.table_name='verification_details' THEN
                EXECUTE 'GRANT UPDATE (validation_status) ON TABLE public.verification_details TO audit_platform_runtime';
            END IF;
        END IF;
    END LOOP;
END $$;

-- Identity sequences are limited to the two permitted inserts.
DO $$
DECLARE
    table_name TEXT;
    sequence_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['uploaded_files','client_data_history'] LOOP
        sequence_name := pg_catalog.pg_get_serial_sequence(format('public.%I', table_name), 'id');
        IF sequence_name IS NULL THEN
            RAISE EXCEPTION 'Expected identity sequence is missing for public.%; transaction rolled back', table_name;
        END IF;
        EXECUTE format('GRANT USAGE ON SEQUENCE %s TO audit_platform_runtime', sequence_name::regclass);
    END LOOP;
END $$;

-- Add the assignment column needed for live role checks without broadening
-- workspace membership or client-card writes.
GRANT SELECT (team_role) ON TABLE public.platform_engagement_assignments TO audit_platform_runtime;

DO $$
DECLARE runtime_oid OID;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_catalog.pg_roles WHERE rolname='audit_platform_runtime';
    IF NOT pg_catalog.has_table_privilege(runtime_oid,'public.client_data','SELECT,DELETE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_data','row_data','UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.client_data','row_data','INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.uploaded_files','status','UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.uploaded_files','file_name','INSERT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.platform_upload_staging','state','UPDATE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.platform_upload_staging','request_id','INSERT')
       OR NOT pg_catalog.has_table_privilege(runtime_oid,'public.platform_upload_jobs','SELECT,INSERT,UPDATE,DELETE')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.platform_engagement_assignments','team_role','SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.platform_client_user_assignments','team_role','SELECT')
       OR NOT pg_catalog.has_column_privilege(runtime_oid,'public.platform_client_user_assignments','is_active','SELECT')
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.clients','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.clients','DELETE'))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.users','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public' AND table_name='users'
                    AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('username','password_hash','is_active'))
                          OR (privilege_type='UPDATE' AND column_name IN ('last_login','username','password_hash'))))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_memberships','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_tenant_memberships' AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('tenant_id','user_id','tenant_role'))
                          OR (privilege_type='UPDATE' AND column_name IN ('tenant_role','is_active'))))
       OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_client_user_assignments','INSERT,UPDATE,DELETE')
       OR EXISTS (SELECT 1 FROM information_schema.column_privileges
                  WHERE grantee='audit_platform_runtime' AND table_schema='public'
                    AND table_name='platform_client_user_assignments' AND privilege_type IN ('INSERT','UPDATE')
                    AND NOT ((privilege_type='INSERT' AND column_name IN ('tenant_id','client_id','user_id','team_role'))
                          OR (privilege_type='UPDATE' AND column_name IN ('team_role','is_active'))))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.platform_tenant_columns','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_columns','DELETE'))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.platform_tenant_templates','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.platform_tenant_templates','DELETE'))
       OR (pg_catalog.has_any_column_privilege(runtime_oid,'public.template_column_mappings','INSERT,UPDATE')
           OR pg_catalog.has_table_privilege(runtime_oid,'public.template_column_mappings','DELETE')) THEN
        RAISE EXCEPTION 'Upload permission verification failed; transaction rolled back';
    END IF;
    IF to_regclass('public.verification_details') IS NOT NULL THEN
        IF NOT pg_catalog.has_column_privilege(runtime_oid,'public.verification_details','validation_status','UPDATE') THEN
            RAISE EXCEPTION 'Review invalidation permission verification failed; transaction rolled back';
        END IF;
    END IF;
END $$;

COMMIT;
