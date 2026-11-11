-- Candidate only: reviewed isolated execution, never startup/live DDL.
-- Legacy saved_column_mappings and canonical current/history stay untouched.
CREATE TABLE public.platform_tenant_saved_mapping_sets (
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    client_id INTEGER NOT NULL REFERENCES public.platform_tenant_client_homes(client_id) ON DELETE RESTRICT,
    template_id INTEGER NOT NULL,
    definition_version BIGINT NOT NULL DEFAULT 1 CHECK(definition_version>0),
    template_binding_key TEXT NOT NULL CHECK(template_binding_key ~ '^[0-9A-F]{64}$'),
    PRIMARY KEY(tenant_id,client_id,template_id),
    FOREIGN KEY(template_id,tenant_id) REFERENCES public.platform_tenant_templates(template_id,tenant_id) ON DELETE RESTRICT
);
CREATE TABLE public.platform_tenant_saved_mappings (
    tenant_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    column_id INTEGER NOT NULL,
    storage_key TEXT NOT NULL,
    excel_column TEXT NOT NULL CHECK(length(btrim(excel_column))>0),
    PRIMARY KEY(tenant_id,client_id,template_id,column_id),
    FOREIGN KEY(tenant_id,client_id,template_id) REFERENCES public.platform_tenant_saved_mapping_sets(tenant_id,client_id,template_id) ON DELETE RESTRICT,
    FOREIGN KEY(tenant_id,column_id) REFERENCES public.platform_tenant_columns(tenant_id,column_id) ON DELETE RESTRICT
);
CREATE INDEX platform_saved_mapping_column ON public.platform_tenant_saved_mappings(column_id);
CREATE INDEX platform_saved_mapping_template ON public.platform_tenant_saved_mappings(template_id);

CREATE FUNCTION public.guard_saved_mapping_set() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Retain mapping revision after clearing entries' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.tenant_id,NEW.client_id,NEW.template_id) IS DISTINCT FROM (OLD.tenant_id,OLD.client_id,OLD.template_id) THEN
            RAISE EXCEPTION 'Mapping scope is immutable' USING ERRCODE='23514';
        END IF;
        NEW.definition_version:=OLD.definition_version+1;
    ELSE NEW.definition_version:=1;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.platform_tenant_client_homes h JOIN public.clients c ON c.id=h.client_id
        WHERE h.tenant_id=NEW.tenant_id AND h.client_id=NEW.client_id AND c.is_active IS TRUE)
       OR NOT EXISTS(SELECT 1 FROM public.platform_tenant_templates d JOIN public.templates t ON t.id=d.template_id
        WHERE d.tenant_id=NEW.tenant_id AND d.template_id=NEW.template_id AND d.binding_origin='native' AND t.is_active IS TRUE) THEN
        RAISE EXCEPTION 'Active native workspace bindings are required' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_saved_mapping_set BEFORE INSERT OR UPDATE OR DELETE ON public.platform_tenant_saved_mapping_sets
    FOR EACH ROW EXECUTE FUNCTION public.guard_saved_mapping_set();

CREATE FUNCTION public.guard_saved_header_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND (NEW.tenant_id,NEW.client_id,NEW.template_id,NEW.column_id,NEW.storage_key)
        IS DISTINCT FROM (OLD.tenant_id,OLD.client_id,OLD.template_id,OLD.column_id,OLD.storage_key) THEN
        RAISE EXCEPTION 'Saved header identity is immutable' USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.template_column_mappings p JOIN public.master_columns m ON m.id=p.column_id
        JOIN public.platform_tenant_columns c ON c.column_id=p.column_id AND c.tenant_id=p.tenant_id
        WHERE p.tenant_id=NEW.tenant_id AND p.template_id=NEW.template_id AND p.column_id=NEW.column_id
            AND p.storage_key=NEW.storage_key AND m.column_name=NEW.storage_key AND m.is_active IS TRUE AND c.binding_origin='native') THEN
        RAISE EXCEPTION 'A current native template column/key is required' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_saved_header_binding BEFORE INSERT OR UPDATE ON public.platform_tenant_saved_mappings
    FOR EACH ROW EXECUTE FUNCTION public.guard_saved_header_binding();
CREATE FUNCTION public.revise_saved_mapping_entries() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE public.platform_tenant_saved_mapping_sets SET definition_version=definition_version
            WHERE tenant_id=OLD.tenant_id AND client_id=OLD.client_id AND template_id=OLD.template_id;
        RETURN OLD;
    END IF;
    UPDATE public.platform_tenant_saved_mapping_sets SET definition_version=definition_version
        WHERE tenant_id=NEW.tenant_id AND client_id=NEW.client_id AND template_id=NEW.template_id;
    RETURN NEW;
END $$;
CREATE TRIGGER revise_saved_mapping_entries AFTER INSERT OR UPDATE OR DELETE ON public.platform_tenant_saved_mappings
    FOR EACH ROW EXECUTE FUNCTION public.revise_saved_mapping_entries();
