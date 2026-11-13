-- Candidate only. Apply explicitly to an owned isolated database; no startup/live DDL.
-- Compact operation outcomes and generated artifact keys, never source-row mirrors.
CREATE TABLE public.platform_storage_cleanup_runs (
    operation_id uuid PRIMARY KEY,
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    user_id integer NOT NULL REFERENCES public.users(id),
    client_id integer NOT NULL REFERENCES public.clients(id),
    template_id integer NOT NULL REFERENCES public.templates(id),
    kind text NOT NULL CHECK(kind IN('purge','plans')),
    request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    fingerprint text NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK(state IN('applying','completed')),
    deleted_counts jsonb NOT NULL DEFAULT '{}' CHECK(octet_length(deleted_counts::text)<=16384),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK((state='completed')=(completed_at IS NOT NULL))
);
CREATE INDEX platform_storage_cleanup_scope ON public.platform_storage_cleanup_runs(tenant_id,client_id,template_id,created_at);
CREATE TABLE public.platform_storage_cleanup_artifacts (
    operation_id uuid NOT NULL REFERENCES public.platform_storage_cleanup_runs(operation_id),
    kind text NOT NULL CHECK(kind IN('local','object','restore')),
    source_key uuid NOT NULL,
    location_hash text NOT NULL CHECK(location_hash ~ '^[0-9a-f]{64}$'),
    source_version bigint NOT NULL DEFAULT 0 CHECK(source_version>=0),
    state text NOT NULL DEFAULT 'pending' CHECK(state IN('pending','working','done','review')),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    lease_version bigint NOT NULL DEFAULT 0 CHECK(lease_version>=0),
    worker_id uuid,
    lease_until timestamptz,
    code text,
    PRIMARY KEY(operation_id,kind,source_key),
    CHECK((state='working')=(worker_id IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX platform_storage_cleanup_pending ON public.platform_storage_cleanup_artifacts(state,lease_until);
CREATE FUNCTION public.guard_storage_cleanup_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NOT EXISTS(SELECT 1 FROM public.platform_storage_cleanup_runs r WHERE r.operation_id=NEW.operation_id AND r.kind='purge' AND r.state='applying')
        THEN RAISE EXCEPTION 'Committed purge preparation required' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' OR ROW(NEW.operation_id,NEW.kind,NEW.source_key,NEW.location_hash,NEW.source_version)
       IS DISTINCT FROM ROW(OLD.operation_id,OLD.kind,OLD.source_key,OLD.location_hash,OLD.source_version)
       OR NEW.lease_version<OLD.lease_version OR NEW.attempts<OLD.attempts OR OLD.state='done' AND NEW IS DISTINCT FROM OLD
    THEN RAISE EXCEPTION 'Cleanup artifact ownership is immutable' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_storage_cleanup_artifact BEFORE INSERT OR UPDATE OR DELETE ON public.platform_storage_cleanup_artifacts
FOR EACH ROW EXECUTE FUNCTION public.guard_storage_cleanup_artifact();
CREATE FUNCTION public.guard_storage_cleanup_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.state='completed' OR
       ROW(NEW.operation_id,NEW.tenant_id,NEW.user_id,NEW.client_id,NEW.template_id,NEW.kind,NEW.request_hash,NEW.fingerprint,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.operation_id,OLD.tenant_id,OLD.user_id,OLD.client_id,OLD.template_id,OLD.kind,OLD.request_hash,OLD.fingerprint,OLD.created_at)
    THEN RAISE EXCEPTION 'Cleanup outcome/identity is immutable' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_storage_cleanup_run BEFORE UPDATE OR DELETE ON public.platform_storage_cleanup_runs
FOR EACH ROW EXECUTE FUNCTION public.guard_storage_cleanup_run();
CREATE FUNCTION public.storage_purge_authorized(tenant integer, client integer, template integer) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS(SELECT 1 FROM public.platform_storage_cleanup_runs r
      WHERE r.operation_id::text=current_setting('auditplatform.purge_operation',true)
        AND r.tenant_id=tenant AND r.client_id=client AND r.template_id=template AND r.kind='purge' AND r.state='applying')
$$;

-- Retain the P16 insert/update guard verbatim; only a matching P33 transaction
-- may delete a scoped receipt. A caller-supplied GUC without its run cannot do so.
CREATE OR REPLACE FUNCTION public.guard_upload_staging() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        IF NOT public.storage_purge_authorized(OLD.tenant_id,OLD.client_id,OLD.template_id)
        THEN RAISE EXCEPTION 'Retain staging receipt' USING ERRCODE='23514'; END IF;
        RETURN OLD;
    END IF;
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
CREATE OR REPLACE FUNCTION public.guard_upload_plan_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'Plan receipt is immutable' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND NOT EXISTS(SELECT 1 FROM public.platform_upload_staging s
        WHERE s.upload_id=NEW.file_id AND s.tenant_id=NEW.tenant_id AND s.user_id=NEW.user_id
          AND s.client_id=NEW.client_id AND s.template_id=NEW.template_id AND s.state='ready'
          AND s.expires_at>=NEW.expires_at) THEN
        RAISE EXCEPTION 'Owned staged source required' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' AND OLD.created_at>now()-interval '24 hours'
       AND NOT public.storage_purge_authorized(OLD.tenant_id,OLD.client_id,OLD.template_id) THEN
        RAISE EXCEPTION 'Retain retry receipt' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
