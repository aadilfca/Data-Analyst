-- Isolated-only P36 candidate. No adoption, amount changes or startup DDL.
CREATE TABLE public.platform_trial_balance_review_operations (
 operation_id uuid PRIMARY KEY,
 tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
 user_id integer NOT NULL REFERENCES public.users(id),
 client_id integer NOT NULL REFERENCES public.clients(id),
 upload_id integer NOT NULL,
 kind text NOT NULL CHECK(kind IN('codes','classification')),
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 fingerprint text NOT NULL CHECK(fingerprint ~ '^[0-9a-f]{64}$'),
 changes jsonb NOT NULL CHECK(jsonb_typeof(changes)='array' AND jsonb_array_length(changes) BETWEEN 1 AND 1000 AND octet_length(changes::text)<=1048576),
 state text NOT NULL DEFAULT 'applying' CHECK(state IN('applying','completed')),
 outcome jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),completed_at timestamptz,
 CHECK((state='applying' AND outcome IS NULL AND completed_at IS NULL) OR
       (state='completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)),
 CHECK(outcome IS NULL OR octet_length(outcome::text)<=16384)
);
CREATE INDEX platform_trial_balance_review_scope ON public.platform_trial_balance_review_operations(tenant_id,client_id,upload_id,created_at);
CREATE FUNCTION public.guard_trial_balance_review_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.state='completed' OR
   ROW(NEW.operation_id,NEW.tenant_id,NEW.user_id,NEW.client_id,NEW.upload_id,NEW.kind,NEW.request_hash,NEW.fingerprint,NEW.changes,NEW.created_at)
   IS DISTINCT FROM ROW(OLD.operation_id,OLD.tenant_id,OLD.user_id,OLD.client_id,OLD.upload_id,OLD.kind,OLD.request_hash,OLD.fingerprint,OLD.changes,OLD.created_at)
 THEN RAISE EXCEPTION 'Trial Balance review evidence is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_trial_balance_review_operation BEFORE UPDATE OR DELETE ON public.platform_trial_balance_review_operations
 FOR EACH ROW EXECUTE FUNCTION public.guard_trial_balance_review_operation();

-- Extend the existing row guard only for a verified review of an owned upload.
-- Preserve its import/deletion rules and forbid all amount/source/adjustment edits.
CREATE OR REPLACE FUNCTION public.guard_native_trial_balance_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE upload integer; owner_client integer; review record;
BEGIN
 IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM public.platform_trial_balance_uploads WHERE upload_id=OLD.upload_id) THEN
   IF TG_TABLE_NAME<>'trial_balance_accounts'
   THEN RAISE EXCEPTION 'Saved adjustments are immutable' USING ERRCODE='23514'; END IF;
   SELECT o.* INTO review FROM public.platform_trial_balance_review_operations o
     JOIN public.platform_trial_balance_uploads n ON n.upload_id=o.upload_id AND n.client_id=o.client_id AND n.tenant_id=o.tenant_id
     JOIN public.platform_tenant_client_homes h ON h.client_id=n.client_id AND h.tenant_id=n.tenant_id
     JOIN public.trial_balance_uploads u ON u.id=n.upload_id AND u.client_id=n.client_id AND u.status='parsed'
     WHERE o.operation_id::text=current_setting('auditplatform.trial_balance_review_operation',true)
       AND o.upload_id=OLD.upload_id AND o.state='applying';
   IF review.operation_id IS NULL OR
     (to_jsonb(NEW)-ARRAY['account_code','parent_account_code','level','classification','manually_overridden'])
       IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['account_code','parent_account_code','level','classification','manually_overridden'])
   THEN RAISE EXCEPTION 'Reviewed code/classification change required' USING ERRCODE='23514'; END IF;
   IF review.kind='codes' THEN
     IF NEW.manually_overridden IS DISTINCT FROM OLD.manually_overridden
       OR OLD.manually_overridden AND NEW.classification IS DISTINCT FROM OLD.classification
       OR NEW.account_code IS DISTINCT FROM OLD.account_code AND NOT EXISTS(
         SELECT 1 FROM jsonb_array_elements(review.changes) c WHERE (c->>'account_id')::integer=OLD.id
           AND c->>'expected_account_code'=OLD.account_code AND c->>'account_code'=NEW.account_code)
     THEN RAISE EXCEPTION 'Code review differs from confirmed changes' USING ERRCODE='23514'; END IF;
   ELSE
     IF ROW(NEW.account_code,NEW.parent_account_code,NEW.level) IS DISTINCT FROM ROW(OLD.account_code,OLD.parent_account_code,OLD.level)
       OR NOT NEW.manually_overridden OR NOT EXISTS(
         SELECT 1 FROM jsonb_array_elements(review.changes) c WHERE (c->>'account_id')::integer=OLD.id
           AND c->>'expected_account_code'=OLD.account_code
           AND NEW.classification=CASE WHEN (c->>'is_leaf')::boolean THEN 'leaf' ELSE 'parent' END)
     THEN RAISE EXCEPTION 'Classification review differs from confirmed changes' USING ERRCODE='23514'; END IF;
   END IF;
   RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN upload=OLD.upload_id; ELSE upload=NEW.upload_id; END IF;
 SELECT client_id INTO owner_client FROM public.platform_trial_balance_uploads WHERE upload_id=upload;
 IF owner_client IS NOT NULL AND NOT public.trial_balance_storage_permit(owner_client,upload)
 THEN RAISE EXCEPTION 'Reviewed native Trial Balance rows required' USING ERRCODE='23514'; END IF;
 IF TG_OP<>'DELETE' AND owner_client IS NOT NULL AND TG_TABLE_NAME='trial_balance_rollforward_adjustments' THEN
   IF NOT EXISTS(SELECT 1 FROM public.trial_balance_accounts a WHERE a.id=NEW.account_id AND a.upload_id=NEW.upload_id)
   THEN RAISE EXCEPTION 'Adjustment account scope required' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
