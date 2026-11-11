-- Reviewed P05B candidate. Execute explicitly in one caller-owned transaction.
-- Runtime startup never runs this file. Shared-schema execution requires the
-- deployed catalog/capacity/ownership approval in POSTGRES_SCHEMA_OWNERSHIP.md.
DO $$ BEGIN
    IF to_regclass('public.client_data') IS NULL THEN
        RAISE EXCEPTION 'Audit migration requires the existing canonical client_data table';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.audit_log (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "user" TEXT NOT NULL, action TEXT NOT NULL, module TEXT NOT NULL,
    details TEXT, "timestamp" TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.data_audit_log (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_data_id INTEGER NOT NULL REFERENCES public.client_data(id) ON DELETE CASCADE,
    "user" TEXT DEFAULT 'System', action TEXT NOT NULL,
    old_row_data JSONB, new_row_data JSONB, changed_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.error_codes (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code TEXT UNIQUE NOT NULL, severity TEXT NOT NULL, category TEXT NOT NULL,
    user_message TEXT NOT NULL, tech_description TEXT, suggested_action TEXT,
    is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.error_log (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    error_code_id INTEGER, error_code TEXT, user_id TEXT DEFAULT 'System',
    client_id INTEGER, module TEXT, endpoint TEXT, method TEXT, status_code INTEGER,
    error_category TEXT, error_message TEXT, file_name TEXT, line_number INTEGER,
    code_context TEXT, stack_trace TEXT, input_data JSONB, suggested_fix TEXT,
    is_resolved BOOLEAN DEFAULT FALSE, resolution_notes TEXT, resolved_by TEXT,
    resolved_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now()
);

-- Refuse legacy types that would require data conversion or guessed timezone
-- interpretation. Preserve existing rows, IDs, defaults, FKs and registry text.
DO $$ DECLARE expected RECORD; actual TEXT; BEGIN
    FOR expected IN SELECT * FROM (VALUES
        ('audit_log','id','integer'), ('audit_log','user','text'),
        ('audit_log','action','text'), ('audit_log','module','text'),
        ('audit_log','details','text'), ('audit_log','timestamp','timestamp with time zone'),
        ('data_audit_log','id','integer'), ('data_audit_log','client_data_id','integer'),
        ('data_audit_log','user','text'), ('data_audit_log','action','text'),
        ('data_audit_log','old_row_data','jsonb'), ('data_audit_log','new_row_data','jsonb'),
        ('data_audit_log','changed_at','timestamp with time zone'),
        ('error_codes','id','integer'), ('error_codes','code','text'),
        ('error_codes','severity','text'), ('error_codes','category','text'),
        ('error_codes','user_message','text'), ('error_codes','tech_description','text'),
        ('error_codes','suggested_action','text'), ('error_codes','is_active','boolean'),
        ('error_codes','created_at','timestamp with time zone'),
        ('error_log','id','integer'), ('error_log','error_code_id','integer'),
        ('error_log','error_code','text'), ('error_log','user_id','text'),
        ('error_log','client_id','integer'), ('error_log','module','text'),
        ('error_log','endpoint','text'), ('error_log','method','text'),
        ('error_log','status_code','integer'), ('error_log','error_category','text'),
        ('error_log','error_message','text'), ('error_log','file_name','text'),
        ('error_log','line_number','integer'), ('error_log','code_context','text'),
        ('error_log','stack_trace','text'), ('error_log','input_data','jsonb'),
        ('error_log','suggested_fix','text'), ('error_log','is_resolved','boolean'),
        ('error_log','resolution_notes','text'), ('error_log','resolved_by','text'),
        ('error_log','resolved_at','timestamp with time zone'), ('error_log','created_at','timestamp with time zone')
    ) AS fields(table_name,column_name,data_type) LOOP
        SELECT data_type INTO actual FROM information_schema.columns
        WHERE table_schema='public' AND table_name=expected.table_name AND column_name=expected.column_name;
        IF actual IS DISTINCT FROM expected.data_type THEN
            RAISE EXCEPTION 'Audit migration found an incompatible legacy column; catalog review is required';
        END IF;
    END LOOP;
END $$;

DO $$ DECLARE target TEXT; BEGIN
    FOREACH target IN ARRAY ARRAY['audit_log','data_audit_log','error_log'] LOOP
        EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS event_id UUID,
            ADD COLUMN IF NOT EXISTS operation_id UUID,
            ADD COLUMN IF NOT EXISTS actor_user_id INTEGER,
            ADD COLUMN IF NOT EXISTS actor_kind TEXT,
            ADD COLUMN IF NOT EXISTS correlation_id TEXT,
            ADD COLUMN IF NOT EXISTS source_origin TEXT,
            ADD COLUMN IF NOT EXISTS source_record_id TEXT,
            ADD COLUMN IF NOT EXISTS source_timestamp TEXT', target);
        -- Old records remain NULL/unclassified; never relabel historical Admin
        -- or System text as a verified authenticated identity.
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=target||'_actor_shape'
            AND conrelid=format('public.%I',target)::regclass) THEN
            EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (CASE
                WHEN actor_kind IS NULL THEN actor_user_id IS NULL
                WHEN actor_kind = ''authenticated'' THEN actor_user_id IS NOT NULL AND actor_user_id > 0
                ELSE actor_kind IN (''anonymous'',''system'',''imported_unknown'') AND actor_user_id IS NULL END)',
                target, target||'_actor_shape');
        END IF;
    END LOOP;
END $$;
ALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS client_id INTEGER;
ALTER TABLE public.data_audit_log ADD COLUMN IF NOT EXISTS client_id INTEGER;
DO $$ DECLARE target TEXT; expected RECORD; actual TEXT; BEGIN
    FOREACH target IN ARRAY ARRAY['audit_log','data_audit_log','error_log'] LOOP
        FOR expected IN SELECT * FROM (VALUES
            ('event_id','uuid'),('operation_id','uuid'),('actor_user_id','integer'),
            ('actor_kind','text'),('client_id','integer'),('correlation_id','text'),
            ('source_origin','text'),('source_record_id','text'),('source_timestamp','text')
        ) AS fields(column_name,data_type) LOOP
            SELECT data_type INTO actual FROM information_schema.columns
            WHERE table_schema='public' AND table_name=target AND column_name=expected.column_name;
            IF actual IS DISTINCT FROM expected.data_type THEN
                RAISE EXCEPTION 'Audit migration found an incompatible additive column; catalog review is required';
            END IF;
        END LOOP;
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_log_event_id ON public.audit_log(event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_data_audit_log_event_id ON public.data_audit_log(event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_error_log_event_id ON public.error_log(event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_data_audit_log_clientdata ON public.data_audit_log(client_data_id);
CREATE INDEX IF NOT EXISTS idx_data_audit_log_action ON public.data_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_error_log_code ON public.error_log(error_code);
CREATE INDEX IF NOT EXISTS idx_error_log_module ON public.error_log(module);
CREATE INDEX IF NOT EXISTS idx_error_log_created ON public.error_log(created_at);
CREATE INDEX IF NOT EXISTS idx_error_log_resolved ON public.error_log(is_resolved);
CREATE INDEX IF NOT EXISTS idx_error_log_category ON public.error_log(error_category);

-- Resolve by stable code string. Never guess its existing numeric ID or replace
-- a site's established message/severity/category when it is already present.
INSERT INTO public.error_codes(code,severity,category,user_message,suggested_action,is_active)
VALUES ('ERR-UNK-001','Error','Unknown','An unexpected error occurred. Please try again or contact support.',
    'Please report this error using the Report Error button.',TRUE)
ON CONFLICT (code) DO NOTHING;
