-- Candidate only. Run once in a reviewed transaction, never at app startup.
-- Existing global templates/mappings keep their IDs, values and ownership.
CREATE TABLE public.platform_tenant_templates (
    template_id INTEGER PRIMARY KEY REFERENCES public.templates(id) ON DELETE RESTRICT,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) > 0),
    template_kind TEXT NOT NULL DEFAULT 'standard' CHECK (template_kind IN ('standard','purchase_order','store_receiving','vendor_bill')),
    binding_origin TEXT NOT NULL DEFAULT 'legacy' CHECK (binding_origin IN ('native','legacy')),
    definition_version BIGINT NOT NULL DEFAULT 1 CHECK (definition_version > 0),
    UNIQUE(template_id,tenant_id)
);
CREATE UNIQUE INDEX platform_tenant_templates_name ON public.platform_tenant_templates(tenant_id,lower(display_name));

-- Nullable for untouched legacy rows; native rows must carry verified bindings.
ALTER TABLE public.template_column_mappings
    ADD COLUMN tenant_id INTEGER,
    ADD COLUMN storage_key TEXT,
    ADD CONSTRAINT tenant_template_owner FOREIGN KEY(template_id,tenant_id)
        REFERENCES public.platform_tenant_templates(template_id,tenant_id) ON DELETE RESTRICT,
    ADD CONSTRAINT tenant_template_column FOREIGN KEY(tenant_id,column_id)
        REFERENCES public.platform_tenant_columns(tenant_id,column_id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX platform_template_column_identity ON public.template_column_mappings(template_id,column_id) WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX platform_template_column_order ON public.template_column_mappings(template_id,sort_order) WHERE tenant_id IS NOT NULL;

CREATE FUNCTION public.guard_tenant_template_definition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Retain tenant template bindings' USING ERRCODE='23514'; END IF;
    IF NEW.template_id<>OLD.template_id OR NEW.tenant_id<>OLD.tenant_id
       OR NEW.binding_origin<>OLD.binding_origin OR NEW.template_kind<>OLD.template_kind THEN
        RAISE EXCEPTION 'Template identity and classification are immutable' USING ERRCODE='23514';
    END IF;
    NEW.definition_version:=OLD.definition_version+1;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_tenant_template_definition BEFORE UPDATE OR DELETE ON public.platform_tenant_templates
    FOR EACH ROW EXECUTE FUNCTION public.guard_tenant_template_definition();

CREATE FUNCTION public.guard_tenant_template_master() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.platform_tenant_templates WHERE template_id=OLD.id) THEN
        IF NEW.id<>OLD.id OR NEW.template_name<>OLD.template_name THEN
            RAISE EXCEPTION 'Template storage identity is immutable' USING ERRCODE='23514';
        END IF;
        UPDATE public.platform_tenant_templates SET definition_version=definition_version
            WHERE template_id=OLD.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_tenant_template_master BEFORE UPDATE ON public.templates
    FOR EACH ROW EXECUTE FUNCTION public.guard_tenant_template_master();

CREATE FUNCTION public.guard_tenant_template_mapping() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id INTEGER; actual_key TEXT;
BEGIN
    IF TG_OP='UPDATE' AND OLD.tenant_id IS NOT NULL AND
       (NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.template_id<>OLD.template_id
        OR NEW.column_id<>OLD.column_id OR NEW.storage_key IS DISTINCT FROM OLD.storage_key) THEN
        RAISE EXCEPTION 'Mapping identity is immutable' USING ERRCODE='23514';
    END IF;
    SELECT tenant_id INTO owner_id FROM public.platform_tenant_templates WHERE template_id=NEW.template_id;
    IF owner_id IS NOT NULL OR NEW.tenant_id IS NOT NULL THEN
        SELECT column_name INTO actual_key FROM public.master_columns WHERE id=NEW.column_id;
        IF owner_id IS NULL OR NEW.tenant_id IS DISTINCT FROM owner_id
           OR NEW.storage_key IS NULL OR NEW.storage_key IS DISTINCT FROM actual_key THEN
            RAISE EXCEPTION 'A verified tenant/key binding is required' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_tenant_template_mapping BEFORE INSERT OR UPDATE ON public.template_column_mappings
    FOR EACH ROW EXECUTE FUNCTION public.guard_tenant_template_mapping();
CREATE FUNCTION public.revise_tenant_template_mapping() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE public.platform_tenant_templates SET definition_version=definition_version WHERE template_id=OLD.template_id;
        RETURN OLD;
    END IF;
    UPDATE public.platform_tenant_templates SET definition_version=definition_version WHERE template_id=NEW.template_id;
    RETURN NEW;
END $$;
CREATE TRIGGER revise_tenant_template_mapping AFTER INSERT OR UPDATE OR DELETE ON public.template_column_mappings
    FOR EACH ROW EXECUTE FUNCTION public.revise_tenant_template_mapping();
