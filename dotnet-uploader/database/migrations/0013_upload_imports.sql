-- Candidate only. Execute explicitly in a transaction after catalog review.
-- No current transaction copy: history alone retains old rows; this receipt is compact.
ALTER TABLE public.client_data_history ADD COLUMN import_operation_id UUID;
ALTER TABLE public.client_data_history ADD COLUMN source_metadata JSONB;
CREATE INDEX idx_client_data_history_import_operation ON public.client_data_history(import_operation_id)
    WHERE import_operation_id IS NOT NULL;

CREATE TABLE public.platform_upload_imports (
    operation_id UUID PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE CHECK(plan_id ~ '^[0-9a-f]{32}$'),
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    client_id INTEGER NOT NULL REFERENCES public.clients(id),
    template_id INTEGER NOT NULL REFERENCES public.templates(id),
    file_id INTEGER NOT NULL REFERENCES public.uploaded_files(id),
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    signature TEXT NOT NULL CHECK(signature ~ '^[0-9a-f]{64}$'),
    outcome JSONB NOT NULL CHECK(jsonb_typeof(outcome)='object' AND octet_length(outcome::text)<=4096),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_platform_upload_imports_scope ON public.platform_upload_imports(tenant_id,client_id,template_id,imported_at);
CREATE FUNCTION public.platform_upload_imports_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Import outcomes are immutable';
END $$;
CREATE TRIGGER platform_upload_imports_no_update BEFORE UPDATE ON public.platform_upload_imports
    FOR EACH ROW EXECUTE FUNCTION public.platform_upload_imports_immutable();
COMMENT ON TABLE public.platform_upload_imports IS 'Compact native import recovery receipts; no transaction row payloads. Scoped permanent purge requires explicit lifecycle handling.';
