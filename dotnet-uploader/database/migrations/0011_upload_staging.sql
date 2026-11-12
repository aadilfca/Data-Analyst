-- Candidate for isolated rehearsals only; no startup/live DDL or legacy adoption.
CREATE TABLE public.platform_upload_staging (
    request_id UUID PRIMARY KEY,
    upload_id INTEGER NOT NULL UNIQUE REFERENCES public.uploaded_files(id) ON DELETE RESTRICT,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    client_id INTEGER NOT NULL REFERENCES public.platform_tenant_client_homes(client_id),
    template_id INTEGER NOT NULL,
    storage_key UUID NOT NULL UNIQUE,
    storage_backend TEXT NOT NULL DEFAULT 'local' CHECK(storage_backend='local'),
    state TEXT NOT NULL DEFAULT 'receiving' CHECK(state IN('receiving','ready','rejected','missing','expired')),
    byte_count BIGINT CHECK(byte_count>0),
    sha256 TEXT CHECK(sha256 ~ '^[0-9a-f]{64}$'),
    discovery JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY(template_id,tenant_id) REFERENCES public.platform_tenant_templates(template_id,tenant_id),
    CHECK(expires_at>created_at),
    CHECK(state<>'ready' OR (byte_count IS NOT NULL AND sha256 IS NOT NULL AND discovery IS NOT NULL))
);
CREATE INDEX platform_upload_staging_expiry ON public.platform_upload_staging(expires_at);
CREATE FUNCTION public.guard_upload_staging() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Retain staging receipt' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' THEN
        IF NOT EXISTS(SELECT 1 FROM public.platform_tenant_client_homes h WHERE h.client_id=NEW.client_id AND h.tenant_id=NEW.tenant_id)
           OR NOT EXISTS(SELECT 1 FROM public.platform_tenant_templates t WHERE t.template_id=NEW.template_id AND t.tenant_id=NEW.tenant_id AND t.binding_origin='native')
           OR NOT EXISTS(SELECT 1 FROM public.uploaded_files f WHERE f.id=NEW.upload_id AND f.client_id=NEW.client_id AND f.template_id=NEW.template_id) THEN
            RAISE EXCEPTION 'Verified staging scope is required' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.request_id,NEW.upload_id,NEW.tenant_id,NEW.user_id,NEW.client_id,NEW.template_id,NEW.storage_key,NEW.storage_backend,NEW.created_at,NEW.expires_at)
       IS DISTINCT FROM (OLD.request_id,OLD.upload_id,OLD.tenant_id,OLD.user_id,OLD.client_id,OLD.template_id,OLD.storage_key,OLD.storage_backend,OLD.created_at,OLD.expires_at) THEN
        RAISE EXCEPTION 'Staging ownership and identity are immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.state<>'receiving' AND NEW.state NOT IN(OLD.state,'missing','expired') THEN
        RAISE EXCEPTION 'Invalid staging transition' USING ERRCODE='23514';
    END IF;
    IF OLD.sha256 IS NOT NULL AND (NEW.sha256,NEW.byte_count) IS DISTINCT FROM (OLD.sha256,OLD.byte_count) THEN
        RAISE EXCEPTION 'Staged content is immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.discovery IS NOT NULL AND NEW.discovery IS DISTINCT FROM OLD.discovery
       AND NOT(NEW.state='expired' AND NEW.discovery IS NULL) THEN
        RAISE EXCEPTION 'Discovery changes require expiry' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_upload_staging BEFORE INSERT OR UPDATE OR DELETE ON public.platform_upload_staging
    FOR EACH ROW EXECUTE FUNCTION public.guard_upload_staging();
