-- Candidate-only P35 ownership/recovery. Execute explicitly in owned isolated
-- tests; no startup/live DDL, amount-type change, or legacy ownership adoption.
CREATE TABLE public.platform_trial_balance_operations (
    operation_id uuid PRIMARY KEY,
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    user_id integer NOT NULL REFERENCES public.users(id),
    client_id integer NOT NULL REFERENCES public.clients(id),
    kind text NOT NULL CHECK(kind IN('import','delete')),
    request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    fingerprint text NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
    source_hash text NOT NULL CHECK(source_hash='' OR source_hash ~ '^[0-9a-f]{64}$'),
    target_upload_ids integer[] NOT NULL,
    result_upload_id integer,
    state text NOT NULL DEFAULT 'applying' CHECK(state IN('applying','completed')),
    outcome jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK((state='completed' AND completed_at IS NOT NULL AND outcome IS NOT NULL)
       OR (state='applying' AND completed_at IS NULL AND outcome IS NULL)),
    CHECK(outcome IS NULL OR octet_length(outcome::text)<=16384)
);
CREATE INDEX platform_trial_balance_operations_scope ON public.platform_trial_balance_operations(tenant_id,client_id,created_at);
CREATE TABLE public.platform_trial_balance_uploads (
    upload_id integer PRIMARY KEY REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    user_id integer NOT NULL REFERENCES public.users(id),
    client_id integer NOT NULL REFERENCES public.clients(id),
    operation_id uuid NOT NULL UNIQUE REFERENCES public.platform_trial_balance_operations(operation_id),
    source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
    review_hash text NOT NULL CHECK(review_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX platform_trial_balance_uploads_scope ON public.platform_trial_balance_uploads(tenant_id,client_id,upload_id);
CREATE FUNCTION public.trial_balance_storage_permit(client integer, upload integer) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS(SELECT 1 FROM public.platform_trial_balance_operations o
      JOIN public.platform_tenant_client_homes h ON h.client_id=o.client_id AND h.tenant_id=o.tenant_id
      WHERE o.operation_id::text=current_setting('auditplatform.trial_balance_operation',true)
        AND o.client_id=client AND o.state='applying'
        AND (upload=ANY(o.target_upload_ids) OR upload=o.result_upload_id))
$$;
CREATE FUNCTION public.guard_trial_balance_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.state='completed' OR
       ROW(NEW.operation_id,NEW.tenant_id,NEW.user_id,NEW.client_id,NEW.kind,NEW.request_hash,NEW.fingerprint,NEW.source_hash,NEW.target_upload_ids,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.operation_id,OLD.tenant_id,OLD.user_id,OLD.client_id,OLD.kind,OLD.request_hash,OLD.fingerprint,OLD.source_hash,OLD.target_upload_ids,OLD.created_at)
       OR OLD.result_upload_id IS NOT NULL AND NEW.result_upload_id IS DISTINCT FROM OLD.result_upload_id
    THEN RAISE EXCEPTION 'Trial Balance operation identity/outcome is immutable' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_trial_balance_operation BEFORE UPDATE OR DELETE ON public.platform_trial_balance_operations
FOR EACH ROW EXECUTE FUNCTION public.guard_trial_balance_operation();
CREATE FUNCTION public.guard_trial_balance_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'Trial Balance ownership is immutable' USING ERRCODE='23514'; END IF;
    IF TG_OP='DELETE' THEN
        IF NOT public.trial_balance_storage_permit(OLD.client_id,OLD.upload_id)
        THEN RAISE EXCEPTION 'Reviewed native Trial Balance deletion required' USING ERRCODE='23514'; END IF;
        RETURN OLD;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.platform_trial_balance_operations o
        JOIN public.trial_balance_uploads u ON u.id=NEW.upload_id AND u.client_id=NEW.client_id
        JOIN public.platform_tenant_client_homes h ON h.client_id=u.client_id AND h.tenant_id=NEW.tenant_id
        WHERE o.operation_id=NEW.operation_id AND o.operation_id::text=current_setting('auditplatform.trial_balance_operation',true)
          AND o.kind='import' AND o.state='applying' AND o.result_upload_id=NEW.upload_id
          AND o.tenant_id=NEW.tenant_id AND o.user_id=NEW.user_id AND o.client_id=NEW.client_id AND o.source_hash=NEW.source_hash)
    THEN RAISE EXCEPTION 'Verified native Trial Balance owner required' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_trial_balance_ownership BEFORE INSERT OR UPDATE OR DELETE ON public.platform_trial_balance_uploads
FOR EACH ROW EXECUTE FUNCTION public.guard_trial_balance_ownership();
CREATE FUNCTION public.guard_native_trial_balance_upload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM public.platform_trial_balance_uploads n WHERE n.upload_id=OLD.id) THEN
        IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
    END IF;
    IF NOT public.trial_balance_storage_permit(OLD.client_id,OLD.id)
    THEN RAISE EXCEPTION 'Reviewed native Trial Balance operation required' USING ERRCODE='23514'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF OLD.status<>'pending' OR NEW.status<>'parsed' OR
       ROW(NEW.id,NEW.client_id,NEW.as_of_date,NEW.file_name,NEW.period_start,NEW.period_end,NEW.period_type,NEW.uploaded_at)
       IS DISTINCT FROM ROW(OLD.id,OLD.client_id,OLD.as_of_date,OLD.file_name,OLD.period_start,OLD.period_end,OLD.period_type,OLD.uploaded_at)
    THEN RAISE EXCEPTION 'Native Trial Balance completion is immutable' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_native_trial_balance_upload BEFORE UPDATE OR DELETE ON public.trial_balance_uploads
FOR EACH ROW EXECUTE FUNCTION public.guard_native_trial_balance_upload();
CREATE FUNCTION public.guard_native_trial_balance_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE upload integer; owner_client integer;
BEGIN
    IF TG_OP='DELETE' THEN upload=OLD.upload_id; ELSE upload=NEW.upload_id; END IF;
    IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM public.platform_trial_balance_uploads WHERE upload_id=OLD.upload_id)
    THEN RAISE EXCEPTION 'Native Trial Balance corrections require their later port' USING ERRCODE='23514'; END IF;
    SELECT client_id INTO owner_client FROM public.platform_trial_balance_uploads WHERE upload_id=upload;
    IF owner_client IS NOT NULL AND NOT public.trial_balance_storage_permit(owner_client,upload)
    THEN RAISE EXCEPTION 'Reviewed native Trial Balance rows required' USING ERRCODE='23514'; END IF;
    IF TG_OP<>'DELETE' AND owner_client IS NOT NULL AND TG_TABLE_NAME='trial_balance_rollforward_adjustments' THEN
        IF NOT EXISTS(SELECT 1 FROM public.trial_balance_accounts a WHERE a.id=NEW.account_id AND a.upload_id=NEW.upload_id)
        THEN RAISE EXCEPTION 'Adjustment account scope required' USING ERRCODE='23514'; END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER guard_native_trial_balance_account BEFORE INSERT OR UPDATE OR DELETE ON public.trial_balance_accounts
FOR EACH ROW EXECUTE FUNCTION public.guard_native_trial_balance_row();
CREATE TRIGGER guard_native_trial_balance_adjustment BEFORE INSERT OR UPDATE OR DELETE ON public.trial_balance_rollforward_adjustments
FOR EACH ROW EXECUTE FUNCTION public.guard_native_trial_balance_row();
