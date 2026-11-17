-- Candidate for an isolated rehearsal only. Never apply at app startup or to a
-- shared database without the approved ownership, capacity and rollback review.
-- Python Audit Rules tables remain unchanged during coexistence.
CREATE SEQUENCE public.platform_audit_rule_form_field_id_seq;

CREATE TABLE public.platform_audit_rule_state (
    tenant_id INTEGER PRIMARY KEY REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    config_version BIGINT NOT NULL DEFAULT 1 CHECK (config_version > 0),
    snapshot_schema_version INTEGER NOT NULL DEFAULT 2 CHECK (snapshot_schema_version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.platform_audit_rule_entities (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('documents','cross-checks','categories','assertions','voucher-checks')),
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition)='object'),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    updated_by INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,entity_kind,id),
    UNIQUE(tenant_id,id)
);
CREATE INDEX platform_audit_rule_entities_list
    ON public.platform_audit_rule_entities(tenant_id,entity_kind,is_active,id);
CREATE UNIQUE INDEX platform_audit_rule_assertion_name
    ON public.platform_audit_rule_entities(tenant_id,(definition->>'name'))
    WHERE entity_kind='assertions';

CREATE TABLE public.platform_audit_rule_changes (
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    config_version BIGINT NOT NULL CHECK (config_version > 1),
    entity_kind TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL CHECK (action IN ('created','updated','deleted','restored','hard_deleted')),
    entity_name TEXT,
    actor_user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,config_version)
);
CREATE INDEX platform_audit_rule_changes_recent
    ON public.platform_audit_rule_changes(tenant_id,config_version DESC);

CREATE TABLE public.platform_audit_rule_snapshots (
    tenant_id INTEGER PRIMARY KEY REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    schema_version INTEGER NOT NULL CHECK (schema_version=2),
    evaluator_version INTEGER NOT NULL CHECK (evaluator_version=1),
    fingerprint CHAR(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_fingerprint CHAR(64) NOT NULL CHECK (evaluation_fingerprint ~ '^[0-9a-f]{64}$'),
    revision BIGINT NOT NULL CHECK (revision > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(tenant_id) REFERENCES public.platform_audit_rule_state(tenant_id) ON DELETE RESTRICT
);

-- A Category snapshot is independently revisioned from the tenant-wide source
-- version. Unchanged Category fingerprints retain their revision on rebuild.
CREATE TABLE public.platform_audit_rule_category_snapshots (
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    entity_kind TEXT NOT NULL DEFAULT 'categories' CHECK (entity_kind='categories'),
    category_id INTEGER NOT NULL,
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    schema_version INTEGER NOT NULL CHECK (schema_version=2),
    evaluator_version INTEGER NOT NULL CHECK (evaluator_version=1),
    fingerprint CHAR(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_fingerprint CHAR(64) NOT NULL CHECK (evaluation_fingerprint ~ '^[0-9a-f]{64}$'),
    revision BIGINT NOT NULL CHECK (revision > 0),
    is_active BOOLEAN NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,category_id),
    FOREIGN KEY(tenant_id,entity_kind,category_id)
        REFERENCES public.platform_audit_rule_entities(tenant_id,entity_kind,id) ON DELETE RESTRICT,
    FOREIGN KEY(tenant_id) REFERENCES public.platform_audit_rule_state(tenant_id) ON DELETE RESTRICT
);
CREATE INDEX platform_audit_rule_category_snapshots_version
    ON public.platform_audit_rule_category_snapshots(tenant_id,config_version DESC);

-- Signature specimens are client-specific, unlike the client-neutral compiled
-- Rulebook. Their document role is still owned by the tenant's Audit Rules.
CREATE TABLE public.platform_audit_rule_signature_specimens (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    client_id INTEGER NOT NULL,
    document_kind TEXT NOT NULL DEFAULT 'documents' CHECK (document_kind='documents'),
    document_id INTEGER NOT NULL,
    role_name TEXT NOT NULL,
    expected_signatory_name TEXT NOT NULL,
    authority TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    updated_by INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,id),
    FOREIGN KEY(client_id,tenant_id)
        REFERENCES public.platform_tenant_client_homes(client_id,tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY(tenant_id,document_kind,document_id)
        REFERENCES public.platform_audit_rule_entities(tenant_id,entity_kind,id) ON DELETE RESTRICT
);
CREATE INDEX platform_audit_rule_signature_specimens_client
    ON public.platform_audit_rule_signature_specimens(tenant_id,client_id,document_id,is_active,role_name,id);
