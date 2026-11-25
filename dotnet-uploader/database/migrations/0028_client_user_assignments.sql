-- Tenant-scoped client access for audit-firm users.
-- Existing active engagement assignments are carried forward only when each
-- user has one consistent role for a client; conflicts stop the migration.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.platform_engagement_assignments a
        JOIN public.platform_audit_engagements e
          ON e.id = a.engagement_id AND e.firm_tenant_id = a.firm_tenant_id
        WHERE a.is_active IS TRUE AND e.is_active IS TRUE
        GROUP BY a.firm_tenant_id, e.client_id, a.user_id
        HAVING count(DISTINCT a.team_role) > 1
    ) THEN
        RAISE EXCEPTION 'Client assignment migration stopped: active engagement roles conflict for a user and client; review assignments before retrying';
    END IF;
END $$;

CREATE TABLE public.platform_client_user_assignments (
    tenant_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team_role TEXT NOT NULL CHECK (team_role IN ('manager', 'internee')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, client_id, user_id),
    FOREIGN KEY (client_id, tenant_id)
        REFERENCES public.platform_tenant_client_homes(client_id, tenant_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES public.platform_tenant_memberships(tenant_id, user_id)
);

CREATE INDEX idx_platform_client_user_assignments_user
    ON public.platform_client_user_assignments (tenant_id, user_id, is_active, client_id);

ALTER TABLE public.platform_client_user_assignments ENABLE ROW LEVEL SECURITY;

INSERT INTO public.platform_client_user_assignments
    (tenant_id, client_id, user_id, team_role)
SELECT a.firm_tenant_id, e.client_id, a.user_id, min(a.team_role)
FROM public.platform_engagement_assignments a
JOIN public.platform_audit_engagements e
  ON e.id = a.engagement_id AND e.firm_tenant_id = a.firm_tenant_id
WHERE a.is_active IS TRUE AND e.is_active IS TRUE
GROUP BY a.firm_tenant_id, e.client_id, a.user_id;

COMMIT;
