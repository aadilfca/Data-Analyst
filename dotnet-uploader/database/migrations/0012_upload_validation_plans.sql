-- Candidate, owned isolated fixtures only. Existing Python plan storage remains
-- authoritative; no startup DDL, adoption, conflict repair or live execution.
CREATE UNIQUE INDEX platform_upload_one_ready_plan
    ON public.upload_validation_plans(client_id,template_id) WHERE status='ready';
CREATE TABLE public.platform_upload_validation_plans (
    request_id UUID PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE CHECK(plan_id ~ '^[0-9a-f]{32}$'),
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    client_id INTEGER NOT NULL REFERENCES public.platform_tenant_client_homes(client_id),
    template_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL REFERENCES public.platform_upload_staging(upload_id),
    envelope TEXT NOT NULL CHECK(octet_length(envelope)<=16384),
    signature TEXT NOT NULL CHECK(signature ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY(template_id,tenant_id) REFERENCES public.platform_tenant_templates(template_id,tenant_id),
    CHECK(expires_at>created_at AND expires_at<=created_at+interval '30 minutes')
);
-- Compact receipts survive supersession/legacy cleanup to reconcile retries.
-- They contain no transaction payload and are removed after a 24-hour window.
CREATE INDEX platform_upload_plan_receipt_age ON public.platform_upload_validation_plans(created_at);
CREATE FUNCTION public.guard_upload_plan_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'Plan receipt is immutable' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND NOT EXISTS(SELECT 1 FROM public.platform_upload_staging s
        WHERE s.upload_id=NEW.file_id AND s.tenant_id=NEW.tenant_id AND s.user_id=NEW.user_id
          AND s.client_id=NEW.client_id AND s.template_id=NEW.template_id AND s.state='ready'
          AND s.expires_at>=NEW.expires_at) THEN
        RAISE EXCEPTION 'Owned staged source required' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' AND OLD.created_at>now()-interval '24 hours' THEN
        RAISE EXCEPTION 'Retain retry receipt' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_upload_plan_receipt BEFORE INSERT OR UPDATE OR DELETE ON public.platform_upload_validation_plans
    FOR EACH ROW EXECUTE FUNCTION public.guard_upload_plan_receipt();
