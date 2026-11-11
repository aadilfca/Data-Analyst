-- P05C local rehearsal candidate. Execute explicitly in an owned test target;
-- runtime startup never applies it. Shared execution retains the schema gate.
DO $$ BEGIN
    IF to_regclass('public.audit_log') IS NULL OR to_regclass('public.data_audit_log') IS NULL
        OR to_regclass('public.error_codes') IS NULL OR to_regclass('public.error_log') IS NULL THEN
        RAISE EXCEPTION 'Reconciliation requires the reviewed audit/error schema';
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS public.log_reconciliation_map (
    source_origin UUID NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('audit_log','data_audit_log','error_log','error_codes')),
    source_record_id INTEGER NOT NULL CHECK(source_record_id > 0),
    target_id INTEGER NOT NULL CHECK(target_id > 0),
    source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
    target_hash TEXT NOT NULL CHECK(target_hash ~ '^[0-9a-f]{64}$'),
    result TEXT NOT NULL CHECK(result IN ('imported','matched')),
    redactions INTEGER NOT NULL CHECK(redactions >= 0),
    source_times JSONB NOT NULL CHECK(jsonb_typeof(source_times)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(source_origin,source_kind,source_record_id)
);
-- This is compact mapping/checkpoint metadata, not a source-record mirror.
