-- Owned isolated fixtures only. Keep existing master IDs/names and all stored
-- transactions untouched. Legacy ownership and dataset bindings are NOT inferred.
-- master_columns.column_name is the fixed storage key for linked native records;
-- the tenant's editable label/category/notes live in this companion definition.
CREATE TABLE public.platform_tenant_columns (
    column_id INTEGER PRIMARY KEY REFERENCES public.master_columns(id) ON DELETE RESTRICT,
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) > 0),
    category TEXT NOT NULL DEFAULT 'General',
    notes TEXT,
    binding_origin TEXT NOT NULL DEFAULT 'legacy' CHECK (binding_origin IN ('native','legacy')),
    definition_version BIGINT NOT NULL DEFAULT 1 CHECK (definition_version > 0),
    UNIQUE (tenant_id,column_id)
);
CREATE UNIQUE INDEX idx_platform_tenant_columns_name
    ON public.platform_tenant_columns(tenant_id,lower(display_name));

CREATE FUNCTION public.platform_guard_column_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Column bindings are retained; deactivate the column' USING ERRCODE='23514';
    END IF;
    IF NEW.column_id IS DISTINCT FROM OLD.column_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.binding_origin IS DISTINCT FROM OLD.binding_origin THEN
        RAISE EXCEPTION 'Column identity and ownership binding are immutable' USING ERRCODE='23514';
    END IF;
    NEW.definition_version := OLD.definition_version + 1;
    RETURN NEW;
END;
$$;
CREATE TRIGGER platform_guard_column_binding
    BEFORE UPDATE OR DELETE ON public.platform_tenant_columns
    FOR EACH ROW EXECUTE FUNCTION public.platform_guard_column_binding();

CREATE FUNCTION public.platform_guard_column_storage_key() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.platform_tenant_columns WHERE column_id=OLD.id) THEN
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.column_name IS DISTINCT FROM OLD.column_name THEN
            RAISE EXCEPTION 'Permanent column ID and storage key cannot be renamed' USING ERRCODE='23514';
        END IF;
        IF NEW.data_type IS DISTINCT FROM OLD.data_type OR NEW.is_active IS DISTINCT FROM OLD.is_active THEN
            UPDATE public.platform_tenant_columns SET definition_version=definition_version WHERE column_id=OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER platform_guard_column_storage_key
    BEFORE UPDATE ON public.master_columns
    FOR EACH ROW EXECUTE FUNCTION public.platform_guard_column_storage_key();
