-- Candidate only. Apply separately after reviewing the fingerprint-bound
-- template-adoption preview. This permits the owner-only adoption tool to
-- promote an exact legacy catalog; it does not change runtime privileges.
CREATE OR REPLACE FUNCTION public.platform_guard_column_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE promotion_authorized boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Column bindings are retained; deactivate the column' USING ERRCODE='23514';
    END IF;
    promotion_authorized := OLD.binding_origin = 'legacy' AND NEW.binding_origin = 'native'
        AND current_user = 'postgres'
        AND coalesce(current_setting('audit.template_adoption_operation',true),'') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    IF NEW.column_id IS DISTINCT FROM OLD.column_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR (NEW.binding_origin IS DISTINCT FROM OLD.binding_origin AND NOT promotion_authorized) THEN
        RAISE EXCEPTION 'Column identity and ownership binding are immutable' USING ERRCODE='23514';
    END IF;
    NEW.definition_version := OLD.definition_version + 1;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.guard_tenant_template_definition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE promotion_authorized boolean;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Retain tenant template bindings' USING ERRCODE='23514';
    END IF;
    promotion_authorized := OLD.binding_origin = 'legacy' AND NEW.binding_origin = 'native'
        AND current_user = 'postgres'
        AND coalesce(current_setting('audit.template_adoption_operation',true),'') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    IF NEW.template_id IS DISTINCT FROM OLD.template_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.template_kind IS DISTINCT FROM OLD.template_kind
       OR (NEW.binding_origin IS DISTINCT FROM OLD.binding_origin AND NOT promotion_authorized) THEN
        RAISE EXCEPTION 'Template identity and classification are immutable' USING ERRCODE='23514';
    END IF;
    NEW.definition_version:=OLD.definition_version+1;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.platform_guard_column_binding() IS
    'Retains immutable tenant column identity; permits owner-only legacy-to-native promotion with a transaction-scoped adoption operation ID.';
COMMENT ON FUNCTION public.guard_tenant_template_definition() IS
    'Retains immutable tenant template identity; permits owner-only legacy-to-native promotion with a transaction-scoped adoption operation ID.';
