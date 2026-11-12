-- Candidate only: explicitly execute in an owned rehearsal transaction.
-- Existing raw values, derived column types and legacy identities are untouched.
CREATE TABLE public.platform_template_upload_bindings (
    template_id INTEGER PRIMARY KEY REFERENCES public.platform_tenant_templates(template_id),
    tenant_id INTEGER NOT NULL REFERENCES public.platform_tenants(id),
    bindings JSONB NOT NULL CHECK(jsonb_typeof(bindings)='object' AND octet_length(bindings::text)<=4096),
    FOREIGN KEY(template_id,tenant_id) REFERENCES public.platform_tenant_templates(template_id,tenant_id)
);
CREATE FUNCTION public.platform_validate_upload_bindings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item RECORD;
BEGIN
    IF NOT EXISTS(SELECT 1 FROM public.platform_tenant_templates t WHERE t.template_id=NEW.template_id
        AND t.tenant_id=NEW.tenant_id AND t.binding_origin='native') THEN RAISE EXCEPTION 'Native scoped template required'; END IF;
    FOR item IN SELECT key,value FROM jsonb_each(NEW.bindings) LOOP
        IF item.key NOT IN('amount','quantity','rate','account_code','account_name','debit','credit','description','line_id')
            OR jsonb_typeof(item.value)<>'number' OR item.value::text !~ '^[1-9][0-9]{0,8}$'
            OR NOT EXISTS(SELECT 1 FROM public.template_column_mappings p JOIN public.platform_tenant_columns c
                ON c.column_id=p.column_id AND c.tenant_id=p.tenant_id JOIN public.master_columns m ON m.id=c.column_id
                WHERE p.template_id=NEW.template_id AND p.tenant_id=NEW.tenant_id AND p.column_id=(item.value::text)::integer
                AND c.binding_origin='native' AND p.storage_key=m.column_name AND m.is_active IS TRUE
                AND (item.key NOT IN('amount','quantity','rate','debit','credit') OR m.data_type='Numeric'))
            THEN RAISE EXCEPTION 'Invalid upload binding'; END IF;
    END LOOP;
    IF NEW.bindings->'rate'=NEW.bindings->'amount' OR NEW.bindings->'rate'=NEW.bindings->'quantity'
        OR NEW.bindings->'debit'=NEW.bindings->'credit' THEN RAISE EXCEPTION 'Distinct semantic columns required'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER platform_upload_bindings_guard BEFORE INSERT OR UPDATE ON public.platform_template_upload_bindings
    FOR EACH ROW EXECUTE FUNCTION public.platform_validate_upload_bindings();
ALTER TABLE public.client_data ADD COLUMN derived_configuration_hash TEXT;
COMMENT ON COLUMN public.client_data.derived_configuration_hash IS 'Native derivation context only; raw JSON remains authoritative. NULL requires review/refresh, never implicit adoption.';
