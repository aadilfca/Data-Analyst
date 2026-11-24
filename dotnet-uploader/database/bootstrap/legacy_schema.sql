-- Review-only empty base schema for the C# migrations 0001-0025.
-- Run only after the permissions plan is approved. This file creates no roles,
-- grants, seed rows, clients, uploads, or transaction data, and applies no C# migrations.
-- Definitions capture the existing PostgreSQL schema used for isolated C# migration rehearsals.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
    ) THEN
        RAISE EXCEPTION 'Expected an empty public schema; review existing objects before using this bootstrap.';
    END IF;
END;
$$;

-- Reusable definitions and client identity tables.
CREATE TABLE public.master_columns (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    column_name TEXT NOT NULL UNIQUE,
    data_type TEXT NOT NULL,
    category TEXT DEFAULT 'General',
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT
);

CREATE TABLE public.templates (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_name TEXT NOT NULL UNIQUE,
    created_by TEXT DEFAULT 'Admin',
    created_date TIMESTAMPTZ DEFAULT now(),
    is_active BOOLEAN DEFAULT TRUE,
    is_system BOOLEAN DEFAULT FALSE,
    has_subtotals_by_default BOOLEAN DEFAULT FALSE,
    -- Required by the current C# template reader; legacy values remain text JSON.
    data_purpose TEXT NOT NULL DEFAULT 'standard',
    ledger_columns TEXT NOT NULL DEFAULT '{}',
    rate_source TEXT
);

CREATE TABLE public.clients (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_code TEXT UNIQUE NOT NULL,
    client_name TEXT NOT NULL,
    industry TEXT,
    contact_person TEXT,
    email TEXT,
    phone TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE public.users (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    role TEXT,
    last_login TIMESTAMPTZ
);

CREATE TABLE public.sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    expiry TIMESTAMPTZ NOT NULL,
    selected_client_id INTEGER REFERENCES public.clients(id) ON DELETE SET NULL
);
CREATE INDEX idx_sessions_user ON public.sessions (user_id);

CREATE TABLE public.template_column_mappings (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES public.templates(id) ON DELETE CASCADE,
    column_id INTEGER NOT NULL REFERENCES public.master_columns(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    reconciliation_role TEXT DEFAULT NULL,
    compare_for_changes BOOLEAN,
    comparison_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (template_id, column_id)
);

-- Upload and canonical data. Later reviewed migrations add their own columns.
CREATE TABLE public.uploaded_files (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT,
    record_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'Pending',
    upload_date TIMESTAMPTZ DEFAULT now(),
    client_id INTEGER REFERENCES public.clients(id) ON DELETE SET NULL,
    template_id INTEGER REFERENCES public.templates(id) ON DELETE SET NULL,
    uploaded_by TEXT DEFAULT 'Analyst',
    file_sha256 TEXT DEFAULT NULL,
    comparison_mode TEXT,
    date_range_start TEXT,
    date_range_end TEXT,
    summary_total_count INTEGER DEFAULT 0,
    summary_added_count INTEGER DEFAULT 0,
    summary_changed_count INTEGER DEFAULT 0,
    summary_removed_count INTEGER DEFAULT 0,
    summary_unchanged_count INTEGER DEFAULT 0,
    summary_updated_at TIMESTAMPTZ
);
CREATE INDEX idx_uploaded_files_source_hash_scope
    ON public.uploaded_files (client_id, template_id, file_sha256, status);

CREATE TABLE public.client_data (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES public.templates(id) ON DELETE CASCADE,
    upload_id INTEGER NOT NULL REFERENCES public.uploaded_files(id) ON DELETE CASCADE,
    row_data JSONB NOT NULL,
    extract_amount REAL,
    extract_date DATE,
    created_at TIMESTAMPTZ DEFAULT now(),
    created_by TEXT DEFAULT 'Analyst',
    is_deleted BOOLEAN DEFAULT FALSE,
    version INTEGER DEFAULT 1,
    data_quality_flag TEXT DEFAULT NULL,
    document_group_id TEXT DEFAULT NULL,
    change_detection_status TEXT DEFAULT 'active',
    rate_source TEXT DEFAULT NULL,
    source_identity TEXT DEFAULT NULL,
    source_fingerprint TEXT DEFAULT NULL,
    source_row_number INTEGER DEFAULT NULL,
    source_order_upload_id INTEGER DEFAULT NULL,
    derived_voucher_id TEXT DEFAULT NULL,
    derived_account_code TEXT DEFAULT NULL,
    derived_account_name TEXT DEFAULT NULL,
    derived_transaction_date DATE DEFAULT NULL,
    derived_transaction_month TEXT DEFAULT NULL,
    derived_transaction_fortnight TEXT DEFAULT NULL,
    derived_debit DOUBLE PRECISION DEFAULT 0,
    derived_credit DOUBLE PRECISION DEFAULT 0,
    derived_treasury_type TEXT DEFAULT NULL,
    derived_treasury_account_code TEXT DEFAULT NULL,
    derived_classification_status TEXT DEFAULT NULL,
    derived_version INTEGER DEFAULT 1
);
CREATE INDEX idx_client_data_client_deleted_extract_date
    ON public.client_data (client_id, is_deleted, extract_date);
CREATE INDEX idx_client_data_client ON public.client_data (client_id);
CREATE INDEX idx_client_data_template ON public.client_data (template_id);
CREATE INDEX idx_client_data_upload ON public.client_data (upload_id);
CREATE INDEX idx_client_data_date ON public.client_data (extract_date);
CREATE INDEX idx_client_data_deleted ON public.client_data (is_deleted);
CREATE INDEX idx_client_data_client_date ON public.client_data (client_id, extract_date);
-- Match the PostgreSQL bootstrap's canonical identity lookups and uniqueness guard.
CREATE INDEX idx_client_data_active_identity
    ON public.client_data (client_id, template_id, is_deleted, source_identity);
CREATE INDEX idx_client_data_active_derived_date
    ON public.client_data (client_id, is_deleted, derived_transaction_date);
CREATE INDEX idx_client_data_active_derived_classification
    ON public.client_data (client_id, is_deleted, derived_treasury_type, derived_transaction_date);
CREATE UNIQUE INDEX idx_client_data_active_identity_unique
    ON public.client_data (client_id, template_id, source_identity)
    WHERE is_deleted = FALSE AND change_detection_status <> 'deleted'
      AND source_identity IS NOT NULL AND source_identity <> '';

CREATE TABLE public.upload_validation_plans (
    plan_id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES public.uploaded_files(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES public.templates(id) ON DELETE CASCADE,
    file_sha256 TEXT NOT NULL,
    request_signature TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'applied', 'expired', 'superseded')),
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ
);
CREATE INDEX idx_upload_validation_plans_expiry
    ON public.upload_validation_plans (expires_at, status);
CREATE INDEX idx_upload_validation_plans_file
    ON public.upload_validation_plans (file_id, status);

CREATE TABLE public.client_data_history (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES public.templates(id) ON DELETE CASCADE,
    client_data_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('changed', 'deleted')),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by TEXT DEFAULT 'System',
    source_upload_id INTEGER,
    replacing_upload_id INTEGER,
    source_identity TEXT,
    row_data JSONB NOT NULL,
    changed_fields JSONB,
    ledger_account_code TEXT,
    ledger_account_name TEXT
);
CREATE INDEX idx_client_data_history_scope_time
    ON public.client_data_history (client_id, template_id, changed_at);

-- Verification prerequisites used by migration 0022. These remain empty.
CREATE TABLE public.audit_rule_documents (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    kb_note TEXT,
    template_id INTEGER REFERENCES public.templates(id),
    is_active BOOLEAN DEFAULT TRUE,
    document_type TEXT NOT NULL DEFAULT 'linked_template'
        CHECK (document_type IN ('linked_template', 'form', 'physical_bill')),
    created_at TIMESTAMPTZ DEFAULT now(),
    risk_rating TEXT,
    transaction_level_approval BOOLEAN DEFAULT FALSE,
    verification_field_policy JSONB,
    is_system BOOLEAN DEFAULT FALSE
);

CREATE TABLE public.audit_rule_document_fields (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    master_column_id INTEGER,
    field_type TEXT NOT NULL DEFAULT 'text',
    field_scope TEXT NOT NULL DEFAULT 'header',
    linked_column TEXT,
    arithmetic_operands TEXT,
    UNIQUE (document_id, field_name)
);
CREATE INDEX idx_audit_rule_document_fields_document
    ON public.audit_rule_document_fields (document_id);

CREATE TABLE public.verification_sessions (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    cashbook_name TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    created_by TEXT DEFAULT 'Analyst',
    created_at TIMESTAMPTZ DEFAULT now(),
    status TEXT DEFAULT 'In Progress',
    total_transactions INTEGER DEFAULT 0,
    verified_count INTEGER DEFAULT 0,
    session_type TEXT NOT NULL DEFAULT 'cash',
    rulebook_version INTEGER
);
CREATE INDEX idx_verification_sessions_client ON public.verification_sessions (client_id);

CREATE TABLE public.verification_details (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES public.verification_sessions(id) ON DELETE CASCADE,
    client_data_id INTEGER NOT NULL REFERENCES public.client_data(id) ON DELETE CASCADE,
    transaction_ref TEXT,
    line_items JSONB,
    procedure_data JSONB,
    validation_status TEXT DEFAULT 'Pending',
    validation_message TEXT,
    status TEXT DEFAULT 'Pending',
    comments TEXT,
    observations TEXT,
    applied_ruleset_hash TEXT,
    verified_by TEXT,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    cleared_status TEXT,
    matrix_version_applied INTEGER,
    range_status TEXT NOT NULL DEFAULT 'active',
    excluded_at TIMESTAMPTZ,
    category_id_applied INTEGER,
    category_name_applied TEXT
);
CREATE INDEX idx_verification_details_session ON public.verification_details (session_id);
CREATE INDEX idx_verification_details_clientdata ON public.verification_details (client_data_id);
CREATE INDEX idx_verification_details_status ON public.verification_details (status);
CREATE INDEX idx_verification_details_validation ON public.verification_details (validation_status);

CREATE TABLE public.voucher_line_form_entries (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_line_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id) ON DELETE CASCADE,
    row_scope TEXT NOT NULL DEFAULT 'header',
    bill_group_index INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_voucher_line_form_entries_line
    ON public.voucher_line_form_entries (voucher_line_id);
CREATE INDEX idx_voucher_line_form_entries_group
    ON public.voucher_line_form_entries (voucher_line_id, document_id, bill_group_index, row_scope, sort_order);

CREATE TABLE public.voucher_line_form_values (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES public.voucher_line_form_entries(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL REFERENCES public.audit_rule_document_fields(id) ON DELETE CASCADE,
    value_text TEXT,
    value_date DATE,
    value_numeric NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (entry_id, field_id)
);
CREATE INDEX idx_voucher_line_form_values_entry ON public.voucher_line_form_values (entry_id);

CREATE TABLE public.audit_rule_voucher_checks (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    authority TEXT
);

CREATE TABLE public.voucher_line_documents (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_line_id INTEGER NOT NULL REFERENCES public.verification_details(id) ON DELETE CASCADE,
    client_data_id INTEGER NOT NULL REFERENCES public.client_data(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id),
    linked_by TEXT DEFAULT 'Analyst',
    linked_at TIMESTAMPTZ DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unassigned')),
    unassigned_at TIMESTAMPTZ,
    signature_exception BOOLEAN DEFAULT FALSE
);
CREATE INDEX idx_voucher_line_documents_line ON public.voucher_line_documents (voucher_line_id);
CREATE INDEX idx_voucher_line_documents_active_line
    ON public.voucher_line_documents (voucher_line_id) WHERE status = 'active';
CREATE UNIQUE INDEX idx_voucher_line_documents_active_client_data
    ON public.voucher_line_documents (client_data_id) WHERE status = 'active';

CREATE TABLE public.voucher_line_procedure_checks (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_line_id INTEGER NOT NULL REFERENCES public.verification_details(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id),
    role_name TEXT NOT NULL,
    is_checked BOOLEAN NOT NULL DEFAULT FALSE,
    checked_by TEXT,
    checked_at TIMESTAMPTZ,
    UNIQUE (voucher_line_id, document_id, role_name)
);
CREATE INDEX idx_voucher_line_procedure_checks_line
    ON public.voucher_line_procedure_checks (voucher_line_id);

CREATE TABLE public.voucher_line_physical_bills (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_line_id INTEGER NOT NULL REFERENCES public.verification_details(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id),
    bill_no TEXT,
    bill_date DATE,
    vendor_name TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_voucher_line_physical_bills_line
    ON public.voucher_line_physical_bills (voucher_line_id);

CREATE TABLE public.voucher_check_status (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    transaction_ref TEXT NOT NULL,
    check_id INTEGER NOT NULL REFERENCES public.audit_rule_voucher_checks(id),
    ticked BOOLEAN NOT NULL DEFAULT FALSE,
    performed_by_name TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    state TEXT NOT NULL DEFAULT 'untouched',
    flagged_in_observations BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (client_id, transaction_ref, check_id)
);
CREATE INDEX idx_voucher_check_status_client_ref
    ON public.voucher_check_status (client_id, transaction_ref);

CREATE TABLE public.voucher_line_observations (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_line_id INTEGER NOT NULL REFERENCES public.verification_details(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES public.audit_rule_documents(id),
    observation_text TEXT,
    synced BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_text TEXT,
    updated_by TEXT DEFAULT 'Analyst',
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (voucher_line_id, document_id)
);
CREATE INDEX idx_voucher_line_observations_line
    ON public.voucher_line_observations (voucher_line_id);

CREATE TABLE public.trial_balance_uploads (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    as_of_date DATE NOT NULL,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'parsed', 'error')),
    row_count INTEGER DEFAULT 0,
    notes TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT now(),
    period_start DATE,
    period_end DATE,
    period_type TEXT
);
CREATE INDEX idx_trial_balance_uploads_client ON public.trial_balance_uploads (client_id);

CREATE TABLE public.trial_balance_accounts (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    upload_id INTEGER NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
    account_code TEXT NOT NULL,
    source_account_code TEXT,
    account_name TEXT,
    parent_account_code TEXT,
    level INTEGER NOT NULL DEFAULT 1,
    classification TEXT NOT NULL CHECK (classification IN ('parent', 'leaf')),
    debit_amount REAL,
    credit_amount REAL,
    opening_dr REAL,
    opening_cr REAL,
    period_dr REAL,
    period_cr REAL,
    closing_dr REAL,
    closing_cr REAL,
    source_account_type TEXT,
    manually_overridden BOOLEAN NOT NULL DEFAULT FALSE,
    category_id INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    raw_row_data JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (upload_id, account_code)
);
CREATE INDEX idx_trial_balance_accounts_upload ON public.trial_balance_accounts (upload_id);
CREATE INDEX idx_trial_balance_accounts_parent
    ON public.trial_balance_accounts (upload_id, parent_account_code);

CREATE TABLE public.trial_balance_rollforward_adjustments (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    upload_id INTEGER NOT NULL REFERENCES public.trial_balance_uploads(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES public.trial_balance_accounts(id) ON DELETE CASCADE,
    source_excel_row INTEGER NOT NULL,
    expected_closing_net NUMERIC NOT NULL,
    original_closing_net NUMERIC NOT NULL,
    difference_net NUMERIC NOT NULL,
    closing_dr_adjustment NUMERIC NOT NULL DEFAULT 0,
    closing_cr_adjustment NUMERIC NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (upload_id, account_id)
);
CREATE INDEX idx_trial_balance_rollforward_adjustments_upload
    ON public.trial_balance_rollforward_adjustments (upload_id);

COMMIT;
