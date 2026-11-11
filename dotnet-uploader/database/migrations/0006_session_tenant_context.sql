-- Owned isolated fixtures only. Shared catalog/mapping/backup/execution gates
-- still apply. Startup does not execute this candidate and no tenant is seeded.
ALTER TABLE public.sessions
    ADD COLUMN selected_tenant_id INTEGER REFERENCES public.platform_tenants(id),
    ADD COLUMN context_version BIGINT NOT NULL DEFAULT 0 CHECK (context_version >= 0);
