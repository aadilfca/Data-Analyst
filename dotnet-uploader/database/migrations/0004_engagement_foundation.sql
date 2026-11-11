-- Candidate for owned isolated fixtures only; shared execution requires the
-- existing catalog, capacity, schema ownership and explicit execution gates.
-- No rows are seeded and existing verification records are not changed.
-- Repeat application fails rather than accepting an unreviewed schema.

-- Composite reference keys preserve the client home and firm type boundaries.
CREATE UNIQUE INDEX idx_platform_tenants_id_type
    ON public.platform_tenants (id, tenant_type);
CREATE UNIQUE INDEX idx_platform_tenant_client_homes_client_tenant
    ON public.platform_tenant_client_homes (client_id, tenant_id);

CREATE TABLE public.platform_audit_engagements (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    firm_tenant_id INTEGER NOT NULL,
    firm_tenant_type TEXT NOT NULL DEFAULT 'audit_firm' CHECK (firm_tenant_type = 'audit_firm'),
    client_id INTEGER NOT NULL,
    name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, firm_tenant_id),
    FOREIGN KEY (firm_tenant_id, firm_tenant_type)
        REFERENCES public.platform_tenants(id, tenant_type),
    FOREIGN KEY (client_id, firm_tenant_id)
        REFERENCES public.platform_tenant_client_homes(client_id, tenant_id)
);

CREATE INDEX idx_platform_audit_engagements_firm_client
    ON public.platform_audit_engagements (firm_tenant_id, client_id);

-- Assignment is eligibility, not permission to perform a particular action.
CREATE TABLE public.platform_engagement_assignments (
    engagement_id INTEGER NOT NULL,
    firm_tenant_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team_role TEXT NOT NULL CHECK (team_role IN ('manager', 'internee')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (engagement_id, user_id),
    FOREIGN KEY (engagement_id, firm_tenant_id)
        REFERENCES public.platform_audit_engagements(id, firm_tenant_id),
    FOREIGN KEY (firm_tenant_id, user_id)
        REFERENCES public.platform_tenant_memberships(tenant_id, user_id)
);

CREATE INDEX idx_platform_engagement_assignments_user
    ON public.platform_engagement_assignments (user_id, firm_tenant_id, engagement_id);
