-- Candidate only. Execute explicitly in a transaction on an owned rehearsal database.
-- Compact completion metadata and row references only; no raw transaction mirrors.
CREATE TABLE public.platform_upload_import_receipts (
    receipt_id UUID PRIMARY KEY,
    operation_id UUID UNIQUE REFERENCES public.platform_upload_imports(operation_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    client_id INTEGER NOT NULL REFERENCES public.clients(id),
    template_id INTEGER NOT NULL REFERENCES public.templates(id),
    file_id INTEGER NOT NULL REFERENCES public.uploaded_files(id),
    kind TEXT NOT NULL CHECK(kind IN('imported','no_changes')),
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    receipt TEXT NOT NULL CHECK(octet_length(receipt)<=524288),
    completed_at TIMESTAMPTZ NOT NULL,
    evidence_expires_at TIMESTAMPTZ NOT NULL CHECK(evidence_expires_at>completed_at),
    CHECK((kind='imported')=(operation_id IS NOT NULL)),
    CHECK(operation_id IS NULL OR operation_id=receipt_id)
);
CREATE INDEX idx_platform_upload_import_receipts_owner ON public.platform_upload_import_receipts(tenant_id,user_id,file_id,completed_at);
CREATE FUNCTION public.platform_upload_import_receipts_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Completion receipts are immutable'; END $$;
CREATE TRIGGER platform_upload_import_receipts_no_update BEFORE UPDATE ON public.platform_upload_import_receipts
    FOR EACH ROW EXECUTE FUNCTION public.platform_upload_import_receipts_immutable();
COMMENT ON TABLE public.platform_upload_import_receipts IS 'Historical technical reconciliation, not audit approval or current-data certification. Detailed evidence is expiry/digest guarded.';
