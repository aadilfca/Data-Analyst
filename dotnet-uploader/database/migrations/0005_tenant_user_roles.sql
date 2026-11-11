-- Candidate only: run against owned isolated fixtures. Shared deployment
-- requires catalog, ownership, recovery and explicit execution approval.
-- Existing memberships receive no administrator privilege automatically.
ALTER TABLE public.platform_tenant_memberships
    ADD COLUMN tenant_role TEXT NOT NULL DEFAULT 'member'
    CHECK (tenant_role IN ('admin', 'member'));
