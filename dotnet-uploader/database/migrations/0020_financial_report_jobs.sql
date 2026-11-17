CREATE TABLE IF NOT EXISTS public.financial_report_jobs (
    job_id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    context_version BIGINT NOT NULL,
    session_hash CHAR(64) NOT NULL CHECK (session_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    request_json JSONB NOT NULL,
    input_fingerprint CHAR(64) NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','preparing_inputs','running','succeeded','failed','cancelled','expired')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    stage TEXT NOT NULL DEFAULT 'queued' CHECK (char_length(stage) BETWEEN 1 AND 40),
    progress_current INTEGER NOT NULL DEFAULT 0 CHECK (progress_current >= 0),
    progress_total INTEGER NOT NULL DEFAULT 3 CHECK (progress_total >= 1 AND progress_current <= progress_total),
    source_ready BOOLEAN NOT NULL DEFAULT false,
    maximum_attempts INTEGER NOT NULL CHECK (maximum_attempts BETWEEN 1 AND 3),
    lease_id UUID,
    lease_until TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    expires_at TIMESTAMPTZ NOT NULL,
    file_name TEXT,
    artifact_content_type TEXT,
    artifact_bytes BYTEA,
    artifact_size INTEGER,
    artifact_sha256 CHAR(64),
    code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((artifact_bytes IS NULL AND artifact_size IS NULL AND artifact_sha256 IS NULL)
        OR (artifact_bytes IS NOT NULL AND artifact_size = octet_length(artifact_bytes)
            AND artifact_size BETWEEN 1 AND 16777216 AND artifact_sha256 ~ '^[0-9a-f]{64}$')),
    UNIQUE (tenant_id,user_id,client_id,request_id)
);

CREATE INDEX IF NOT EXISTS idx_financial_report_jobs_queue
    ON public.financial_report_jobs(available_at,created_at)
    WHERE state IN ('queued','preparing_inputs','running');
CREATE INDEX IF NOT EXISTS idx_financial_report_jobs_owner
    ON public.financial_report_jobs(tenant_id,user_id,client_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_financial_report_jobs_expiry
    ON public.financial_report_jobs(expires_at)
    WHERE state IN ('succeeded','failed','cancelled');
