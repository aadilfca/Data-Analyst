-- Candidate only: owned isolated fixtures. No startup DDL or legacy adoption.
CREATE TABLE public.platform_upload_jobs (
    job_id UUID PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    user_id INTEGER NOT NULL REFERENCES public.users(id),
    client_id INTEGER NOT NULL REFERENCES public.clients(id),
    template_id INTEGER NOT NULL REFERENCES public.templates(id),
    file_id INTEGER NOT NULL REFERENCES public.uploaded_files(id),
    kind TEXT NOT NULL CHECK(kind IN('import','no_changes')),
    plan_id TEXT UNIQUE,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    request_text TEXT NOT NULL CHECK(octet_length(request_text)<=1048576),
    session_hash TEXT NOT NULL CHECK(session_hash ~ '^[0-9a-f]{64}$'),
    context_version BIGINT NOT NULL,
    source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
    storage_hash TEXT NOT NULL CHECK(storage_hash ~ '^[0-9a-f]{64}$'),
    source_bytes BIGINT NOT NULL CHECK(source_bytes BETWEEN 1 AND 67108864),
    source_expires_at TIMESTAMPTZ NOT NULL,
    source_ready BOOLEAN NOT NULL DEFAULT FALSE,
    source_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    state TEXT NOT NULL DEFAULT 'storing_source' CHECK(state IN
        ('storing_source','queued','running','retry_wait','recovering','authorization_required','review_required','succeeded','failed','cancelled','expired')),
    stage TEXT NOT NULL DEFAULT 'storing_source',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    lease_version BIGINT NOT NULL DEFAULT 0 CHECK(lease_version>=0),
    worker_id UUID,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    code TEXT,
    completion TEXT CHECK(completion IS NULL OR octet_length(completion)<=131072),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK(expires_at<=source_expires_at),
    CHECK((state='running')=(worker_id IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK((worker_id IS NULL)=(lease_until IS NULL)),
    CHECK((state='succeeded')=(completion IS NOT NULL)),
    CHECK(kind='import' AND plan_id IS NOT NULL OR kind='no_changes' AND plan_id IS NULL)
);
CREATE INDEX platform_upload_jobs_claim ON public.platform_upload_jobs(state,next_attempt_at,lease_until);
CREATE INDEX platform_upload_jobs_owner ON public.platform_upload_jobs(tenant_id,user_id,client_id,template_id,created_at);
CREATE FUNCTION public.platform_upload_job_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.job_id,NEW.tenant_id,NEW.user_id,NEW.client_id,NEW.template_id,NEW.file_id,NEW.kind,NEW.plan_id,
        NEW.input_hash,NEW.request_text,NEW.source_hash,NEW.storage_hash,NEW.source_bytes,NEW.source_expires_at,NEW.created_at,NEW.expires_at)
       IS DISTINCT FROM ROW(OLD.job_id,OLD.tenant_id,OLD.user_id,OLD.client_id,OLD.template_id,OLD.file_id,OLD.kind,OLD.plan_id,
        OLD.input_hash,OLD.request_text,OLD.source_hash,OLD.storage_hash,OLD.source_bytes,OLD.source_expires_at,OLD.created_at,OLD.expires_at)
       OR NEW.lease_version<OLD.lease_version OR NEW.attempts<OLD.attempts
       OR OLD.completion IS NOT NULL AND NEW.completion IS DISTINCT FROM OLD.completion
    THEN RAISE EXCEPTION 'upload job identity or completion is immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER platform_upload_job_identity BEFORE UPDATE ON public.platform_upload_jobs
    FOR EACH ROW EXECUTE FUNCTION public.platform_upload_job_identity_guard();
