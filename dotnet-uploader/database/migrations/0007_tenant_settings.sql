-- Candidate for owned isolated fixtures only. No legacy settings are copied,
-- no tenants/default rows are seeded, and runtime startup performs no DDL.
CREATE TABLE public.platform_tenant_settings (
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
);
CREATE TABLE public.platform_tenant_holidays (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    holiday_date DATE NOT NULL,
    label TEXT NOT NULL CHECK (length(btrim(label)) > 0),
    repeats_yearly BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_platform_tenant_holidays_date
    ON public.platform_tenant_holidays(tenant_id,holiday_date,id);
