-- P57 candidate: explicit application by the approved operator only, never at startup.
-- NULL preserves unknown historical ownership. No inferred backfill or data transfer.
ALTER TABLE public.error_log ADD COLUMN tenant_id INTEGER
    REFERENCES public.platform_tenants(id) ON DELETE RESTRICT;
CREATE INDEX idx_error_log_tenant_created ON public.error_log(tenant_id,created_at DESC,id DESC);
CREATE INDEX idx_error_log_tenant_resolution ON public.error_log(tenant_id,is_resolved);
