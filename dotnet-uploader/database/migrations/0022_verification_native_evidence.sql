-- Candidate only. Apply only in the reviewed deployment migration sequence.
-- Legacy Python rows keep their ID namespace in rulebook_source='legacy'; native rows use
-- 'native'. Tenant ownership is backfilled where possible; JSON entity IDs stay app-validated.

ALTER TABLE public.voucher_line_documents
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_line_documents
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));
ALTER TABLE public.voucher_line_procedure_checks
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_line_procedure_checks
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));
ALTER TABLE public.voucher_line_observations
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_line_observations
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));
ALTER TABLE public.voucher_line_form_entries
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_line_form_entries
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));
ALTER TABLE public.voucher_line_physical_bills
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_line_physical_bills
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));
ALTER TABLE public.voucher_check_status
    ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
ALTER TABLE public.voucher_check_status
    ADD COLUMN IF NOT EXISTS rulebook_source TEXT NOT NULL DEFAULT 'legacy' CHECK (rulebook_source IN ('legacy','native'));

UPDATE public.voucher_line_documents x SET tenant_id=h.tenant_id
FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
WHERE x.voucher_line_id=d.id AND x.tenant_id IS NULL;
UPDATE public.voucher_line_procedure_checks x SET tenant_id=h.tenant_id
FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
WHERE x.voucher_line_id=d.id AND x.tenant_id IS NULL;
UPDATE public.voucher_line_observations x SET tenant_id=h.tenant_id
FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
WHERE x.voucher_line_id=d.id AND x.tenant_id IS NULL;
UPDATE public.voucher_line_form_entries x SET tenant_id=h.tenant_id
FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
WHERE x.voucher_line_id=d.id AND x.tenant_id IS NULL;
UPDATE public.voucher_line_physical_bills x SET tenant_id=h.tenant_id
FROM public.verification_details d JOIN public.verification_sessions s ON s.id=d.session_id
JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
WHERE x.voucher_line_id=d.id AND x.tenant_id IS NULL;
UPDATE public.voucher_check_status x SET tenant_id=h.tenant_id
FROM public.platform_tenant_client_homes h
WHERE x.client_id=h.client_id AND x.tenant_id IS NULL;

-- Native Rulebook definitions are JSON entities, not rows in Python's legacy tables.
-- Remove only the legacy-definition foreign keys; keep line, source-row and tenant ownership.
ALTER TABLE public.voucher_line_documents
    DROP CONSTRAINT IF EXISTS voucher_line_documents_document_id_fkey;
ALTER TABLE public.voucher_line_procedure_checks
    DROP CONSTRAINT IF EXISTS voucher_line_procedure_checks_document_id_fkey;
ALTER TABLE public.voucher_line_observations
    DROP CONSTRAINT IF EXISTS voucher_line_observations_document_id_fkey;
ALTER TABLE public.voucher_line_form_entries
    DROP CONSTRAINT IF EXISTS voucher_line_form_entries_document_id_fkey;
ALTER TABLE public.voucher_line_physical_bills
    DROP CONSTRAINT IF EXISTS voucher_line_physical_bills_document_id_fkey;
ALTER TABLE public.voucher_line_form_values
    DROP CONSTRAINT IF EXISTS voucher_line_form_values_field_id_fkey;
ALTER TABLE public.voucher_check_status
    DROP CONSTRAINT IF EXISTS voucher_check_status_check_id_fkey;

-- Preserve the old uniqueness boundary for legacy records and give native rows
-- tenant-qualified identity. Deploy the compatible Python conflict predicates
-- and restart its process before any legacy rollback; old SQL cannot infer these
-- partial indexes. Mixed-backend read/write compatibility needs separate proof.
-- PostgreSQL shortens generated constraint names by truncating individual name
-- components, so resolve this exact column set instead of guessing its name.
DO $$ DECLARE old_unique RECORD; BEGIN
    FOR old_unique IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid='public.voucher_line_procedure_checks'::regclass
          AND c.contype='u'
          AND ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(attnum,position)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
                    ORDER BY k.position)=ARRAY['voucher_line_id','document_id','role_name']::text[]
    LOOP
        EXECUTE format('ALTER TABLE public.voucher_line_procedure_checks DROP CONSTRAINT %I',old_unique.conname);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS voucher_line_procedure_checks_legacy_identity
    ON public.voucher_line_procedure_checks(voucher_line_id,document_id,role_name) WHERE rulebook_source='legacy';
CREATE UNIQUE INDEX IF NOT EXISTS voucher_line_procedure_checks_native_identity
    ON public.voucher_line_procedure_checks(voucher_line_id,tenant_id,document_id,role_name) WHERE rulebook_source='native';

ALTER TABLE public.voucher_line_observations
    DROP CONSTRAINT IF EXISTS voucher_line_observations_voucher_line_id_document_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS voucher_line_observations_legacy_identity
    ON public.voucher_line_observations(voucher_line_id,document_id) WHERE rulebook_source='legacy';
CREATE UNIQUE INDEX IF NOT EXISTS voucher_line_observations_native_identity
    ON public.voucher_line_observations(voucher_line_id,tenant_id,document_id) WHERE rulebook_source='native';

ALTER TABLE public.voucher_check_status
    DROP CONSTRAINT IF EXISTS voucher_check_status_client_id_transaction_ref_check_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS voucher_check_status_legacy_identity
    ON public.voucher_check_status(client_id,transaction_ref,check_id) WHERE rulebook_source='legacy';
CREATE UNIQUE INDEX IF NOT EXISTS voucher_check_status_native_identity
    ON public.voucher_check_status(tenant_id,client_id,transaction_ref,check_id) WHERE rulebook_source='native';

CREATE INDEX IF NOT EXISTS voucher_line_documents_tenant_line
    ON public.voucher_line_documents(tenant_id,voucher_line_id);
CREATE INDEX IF NOT EXISTS voucher_line_procedure_checks_tenant_line
    ON public.voucher_line_procedure_checks(tenant_id,voucher_line_id);
CREATE INDEX IF NOT EXISTS voucher_line_observations_tenant_line
    ON public.voucher_line_observations(tenant_id,voucher_line_id);
CREATE INDEX IF NOT EXISTS voucher_line_form_entries_tenant_line_document
    ON public.voucher_line_form_entries(tenant_id,voucher_line_id,document_id);
CREATE INDEX IF NOT EXISTS voucher_line_physical_bills_tenant_line_document
    ON public.voucher_line_physical_bills(tenant_id,voucher_line_id,document_id);
CREATE INDEX IF NOT EXISTS voucher_check_status_tenant_client_ref
    ON public.voucher_check_status(tenant_id,client_id,transaction_ref);

COMMENT ON COLUMN public.voucher_line_documents.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 JSON entity IDs.';
COMMENT ON COLUMN public.voucher_line_procedure_checks.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 JSON entity IDs.';
COMMENT ON COLUMN public.voucher_line_observations.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 JSON entity IDs.';
COMMENT ON COLUMN public.voucher_line_form_entries.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 Document and Form-field IDs.';
COMMENT ON COLUMN public.voucher_line_physical_bills.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 Document IDs.';
COMMENT ON COLUMN public.voucher_check_status.rulebook_source IS 'legacy preserves Python Rulebook identity; native uses tenant-owned P41 Voucher Check IDs.';
