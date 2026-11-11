-- Candidate for isolated rehearsal only. Do not apply to a shared database
-- without the catalog, capacity, ownership, recovery and execution gates.
-- No rows are seeded and no existing records or permissions are changed.
-- A repeat or incompatible existing object fails; migration tracking is a
-- separate deployment responsibility, not an automatic startup behavior.
CREATE TABLE public.platform_tenants (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    tenant_type TEXT NOT NULL CHECK (tenant_type IN ('business', 'audit_firm')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.platform_tenant_memberships (
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE INDEX idx_platform_tenant_memberships_user
    ON public.platform_tenant_memberships (user_id, tenant_id);

-- A home workspace association is not a grant or a legal-ownership claim.
-- Cross-tenant sharing and engagement permissions are separately designed.
CREATE TABLE public.platform_tenant_client_homes (
    client_id INTEGER PRIMARY KEY REFERENCES public.clients(id),
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_platform_tenant_client_homes_tenant
    ON public.platform_tenant_client_homes (tenant_id, client_id);
