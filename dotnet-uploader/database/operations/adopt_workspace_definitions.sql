-- Adopt the already-copied reusable definitions for the first C# workspace.
-- This links definitions only; it does not copy clients, transactions, or users.
-- Run once, after 0008_tenant_columns.sql and 0009_tenant_templates.sql.
BEGIN;

DO $$
DECLARE
    workspace_id INTEGER;
BEGIN
    IF current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'Unexpected database; no changes applied';
    END IF;

    SELECT id INTO workspace_id
    FROM public.platform_tenants
    WHERE name = 'AadilCA' AND is_active IS TRUE;
    IF workspace_id IS NULL OR (SELECT count(*) FROM public.platform_tenants WHERE name = 'AadilCA') <> 1 THEN
        RAISE EXCEPTION 'Expected exactly one active AadilCA workspace; no changes applied';
    END IF;

    IF (SELECT count(*) FROM public.master_columns) <> 78
       OR (SELECT count(*) FROM public.templates) <> 10
       OR (SELECT count(*) FROM public.template_column_mappings) <> 93 THEN
        RAISE EXCEPTION 'Definition counts differ from the reviewed transfer (78/10/93); no changes applied';
    END IF;

    IF (SELECT count(*) FROM public.platform_tenant_columns) <> 0
       OR (SELECT count(*) FROM public.platform_tenant_templates) <> 0
       OR EXISTS (SELECT 1 FROM public.template_column_mappings WHERE tenant_id IS NOT NULL OR storage_key IS NOT NULL) THEN
        RAISE EXCEPTION 'Tenant definition links already exist; inspect before retrying';
    END IF;

    IF (SELECT count(*) FROM public.template_column_mappings p
        LEFT JOIN public.templates t ON t.id = p.template_id
        LEFT JOIN public.master_columns c ON c.id = p.column_id
        WHERE t.id IS NULL OR c.id IS NULL) <> 0 THEN
        RAISE EXCEPTION 'A definition mapping has a missing template or column; no changes applied';
    END IF;

    IF (SELECT count(DISTINCT lower(btrim(column_name))) FROM public.master_columns) <> 78
       OR (SELECT count(DISTINCT lower(btrim(template_name))) FROM public.templates) <> 10 THEN
        RAISE EXCEPTION 'Definitions contain duplicate or blank names; no changes applied';
    END IF;
END $$;

INSERT INTO public.platform_tenant_columns
    (column_id, tenant_id, display_name, category, notes, binding_origin)
SELECT c.id, t.id, c.column_name, COALESCE(NULLIF(btrim(c.category), ''), 'General'), c.notes, 'legacy'
FROM public.master_columns c
CROSS JOIN (SELECT id FROM public.platform_tenants WHERE name = 'AadilCA') t;

INSERT INTO public.platform_tenant_templates
    (template_id, tenant_id, display_name, template_kind, binding_origin)
SELECT x.id, t.id, x.template_name,
       CASE lower(btrim(x.template_name))
           WHEN 'purchase order' THEN 'purchase_order'
           WHEN 'store receiving' THEN 'store_receiving'
           WHEN 'vendor bill' THEN 'vendor_bill'
           ELSE 'standard'
       END,
       'legacy'
FROM public.templates x
CROSS JOIN (SELECT id FROM public.platform_tenants WHERE name = 'AadilCA') t;

UPDATE public.template_column_mappings p
SET tenant_id = t.id, storage_key = c.column_name
FROM public.platform_tenants t, public.master_columns c
WHERE t.name = 'AadilCA' AND c.id = p.column_id
  AND p.tenant_id IS NULL AND p.storage_key IS NULL;

DO $$
DECLARE workspace_id INTEGER;
BEGIN
    SELECT id INTO workspace_id FROM public.platform_tenants WHERE name = 'AadilCA';
    IF (SELECT count(*) FROM public.platform_tenant_columns WHERE tenant_id = workspace_id) <> 78
       OR (SELECT count(*) FROM public.platform_tenant_templates WHERE tenant_id = workspace_id) <> 10
       OR (SELECT count(*) FROM public.template_column_mappings WHERE tenant_id = workspace_id) <> 93
       OR EXISTS (
           SELECT 1 FROM public.template_column_mappings p
           JOIN public.master_columns c ON c.id = p.column_id
           WHERE p.tenant_id = workspace_id AND p.storage_key IS DISTINCT FROM c.column_name
       ) THEN
        RAISE EXCEPTION 'Workspace definition verification failed; transaction rolled back';
    END IF;
END $$;

COMMIT;
