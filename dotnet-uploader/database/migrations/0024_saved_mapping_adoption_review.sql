-- Candidate only. Compact references preserve unresolved legacy choices without
-- copying workbook values or changing inactive clients/deleted column bindings.
CREATE TABLE public.platform_saved_mapping_adoption_review (
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    source_mapping_id integer NOT NULL,
    client_id integer NOT NULL,
    template_id integer NOT NULL,
    reason text NOT NULL CHECK(reason IN ('inactive_client','missing_column_binding')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,source_mapping_id),
    FOREIGN KEY(client_id,tenant_id)
        REFERENCES public.platform_tenant_client_homes(client_id,tenant_id),
    FOREIGN KEY(template_id,tenant_id)
        REFERENCES public.platform_tenant_templates(template_id,tenant_id)
);
CREATE INDEX platform_saved_mapping_adoption_review_scope
    ON public.platform_saved_mapping_adoption_review(tenant_id,client_id,template_id);
COMMENT ON TABLE public.platform_saved_mapping_adoption_review IS
    'Unresolved legacy saved-header references; cleared only by an explicit scoped native mapping save/clear. Original legacy rows remain untouched.';
