-- Native C# schema for client-scoped Verification and account-control routing.
-- Rows are created and maintained by the C# Trial Balance review workflow.
CREATE TABLE IF NOT EXISTS public.account_control_assignments (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    account_code TEXT NOT NULL,
    verification_workstream TEXT,
    financial_statement_category_id INTEGER,
    note_category_id INTEGER,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT account_control_assignments_client_account_key UNIQUE (client_id, account_code)
);
