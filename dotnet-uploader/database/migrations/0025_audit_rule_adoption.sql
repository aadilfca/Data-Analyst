-- Candidate only: explicit owned-local Rulebook adoption. Original definitions,
-- frozen legacy snapshots, sessions and saved evidence remain untouched.
CREATE TABLE public.platform_audit_rule_adoption_map (
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    entity_kind text NOT NULL,
    source_id integer NOT NULL CHECK(source_id>0),
    entity_id integer NOT NULL,
    source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    source_is_system boolean NOT NULL DEFAULT false,
    PRIMARY KEY(tenant_id,entity_kind,source_id),
    UNIQUE(tenant_id,entity_id),
    FOREIGN KEY(tenant_id,entity_kind,entity_id)
        REFERENCES public.platform_audit_rule_entities(tenant_id,entity_kind,id)
);
CREATE TABLE public.platform_audit_rule_form_adoption_map (
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    source_field_id integer NOT NULL CHECK(source_field_id>0),
    document_kind text NOT NULL DEFAULT 'documents' CHECK(document_kind='documents'),
    document_id integer NOT NULL,
    field_id integer NOT NULL CHECK(field_id>0),
    PRIMARY KEY(tenant_id,source_field_id),
    UNIQUE(tenant_id,field_id),
    FOREIGN KEY(tenant_id,document_kind,document_id)
        REFERENCES public.platform_audit_rule_entities(tenant_id,entity_kind,id)
);
CREATE TABLE public.platform_audit_rule_specimen_adoption_map (
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    source_id integer NOT NULL CHECK(source_id>0),
    specimen_id integer NOT NULL,
    source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(tenant_id,source_id),
    UNIQUE(tenant_id,specimen_id),
    FOREIGN KEY(tenant_id,specimen_id)
        REFERENCES public.platform_audit_rule_signature_specimens(tenant_id,id)
);
CREATE TABLE public.platform_audit_rule_adoption_review (
    tenant_id integer NOT NULL REFERENCES public.platform_tenants(id),
    source_kind text NOT NULL CHECK(source_kind IN ('signature_specimens','document_fields')),
    source_id integer NOT NULL CHECK(source_id>0),
    reason text NOT NULL CHECK(reason IN ('missing_document_role','non_form_field_definition')),
    source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(tenant_id,source_kind,source_id)
);
COMMENT ON TABLE public.platform_audit_rule_adoption_map IS
    'Legacy kind-qualified IDs to native global IDs. Historical evidence is not retagged or rebased by definition adoption.';
