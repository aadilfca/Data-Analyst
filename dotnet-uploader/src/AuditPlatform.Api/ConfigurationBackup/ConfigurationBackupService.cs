using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.Settings;
using System.Security.Cryptography;
using System.Text.Json;
using NpgsqlTypes;
using Npgsql;

namespace AuditPlatform.Api.ConfigurationBackup;

// A configuration backup describes existing identities in one workspace.
// It never contains transaction rows or grants access to another workspace.
public sealed record BackupMappingEntry(int ColumnId, string StorageKey, string Header);
public sealed record BackupMapping(int ClientId, int TemplateId, string BindingKey, long Revision,
    IReadOnlyList<BackupMappingEntry> Entries);
public sealed record BackupHoliday(int Id, DateOnly Date, string Label, bool RepeatsYearly);
public sealed record ConfigurationBackupDocument(int Version, int TenantId,
    IReadOnlyDictionary<string, string> Settings, IReadOnlyList<TenantColumn> Columns,
    IReadOnlyList<TemplateDefinition> Templates, IReadOnlyList<BackupMapping> Mappings,
    IReadOnlyList<BackupHoliday> Holidays);
public sealed record BackupResult(string Status, string? Detail = null, string? Revision = null);
public sealed class BackupReviewException(string message) : Exception(message);

public static class ConfigurationBackupValidation
{
    // Reject malformed, duplicate and unbound data before any restore writes.
    public static bool IsValid(ConfigurationBackupDocument? document, int tenantId)
    {
        if (document is null || document.Version != 1 || document.TenantId != tenantId || tenantId <= 0
            || document.Settings is null || document.Columns is null || document.Templates is null || document.Mappings is null || document.Holidays is null
            || document.Settings.Count>100 || document.Columns.Count>5000 || document.Templates.Count>2000
            || document.Mappings.Count>10000 || document.Holidays.Count>10000
            || document.Settings.Any(p=>p.Key is null || p.Value is null || p.Key.Contains('\0') || p.Value.Contains('\0'))
            || document.Holidays.Any(h=>h is null || h.Id<=0 || string.IsNullOrWhiteSpace(h.Label) || h.Label.Contains('\0'))
            || document.Holidays.Select(h=>h.Id).Distinct().Count()!=document.Holidays.Count
            || document.Columns.Any(c => c is null || c.Id <= 0 || c.Origin != "native" || string.IsNullOrWhiteSpace(c.Name)
                || c.Name.Contains('\0') || c.Category is null || c.Category.Contains('\0') || c.Notes?.Contains('\0')==true
                || string.IsNullOrWhiteSpace(c.StorageKey) || c.StorageKey.Contains('\0') || c.Type is not ("Text" or "Numeric" or "Date"))
            || document.Columns.Select(c => c.Id).Distinct().Count() != document.Columns.Count
            || document.Templates.Any(t => t is null || t.Id <= 0 || t.Origin != "native" || t.Columns is null || t.Ledger is null)
            || document.Templates.Select(t => t.Id).Distinct().Count() != document.Templates.Count
            || document.Mappings.Any(m => m is null || m.ClientId <= 0 || m.TemplateId <= 0 || m.Revision <= 0 || m.Entries is null
                || m.BindingKey is null || m.BindingKey.Length!=64 || m.BindingKey.Any(c=>!(c is >= '0' and <= '9' or >= 'A' and <= 'F'))
                || m.Entries.Any(e => e is null || e.ColumnId <= 0 || string.IsNullOrWhiteSpace(e.StorageKey)
                    || string.IsNullOrWhiteSpace(e.Header) || e.Header.Contains('\0'))
                || m.Entries.Select(e => e.ColumnId).Distinct().Count() != m.Entries.Count)
            || document.Mappings.Select(m => (m.ClientId, m.TemplateId)).Distinct().Count() != document.Mappings.Count)
            return false;

        var columns = document.Columns.ToDictionary(c => c.Id);
        foreach (var template in document.Templates)
        {
            if (string.IsNullOrWhiteSpace(template.Name) || template.Name.Contains('\0') || template.Name.Trim().Length>200 || template.Columns.Count==0
                || template.Kind is not ("standard" or "purchase_order" or "store_receiving" or "vendor_bill")
                || (template.Kind=="standard" || template.Purpose=="account_ledger" ? template.RateSource is not null
                    : template.RateSource is not ("auto" or "imported" or "derived"))
                || template.Columns.Any(c => c is null || !columns.TryGetValue(c.Id, out var column)
                    || column.StorageKey != c.StorageKey || column.Type != c.Type || !column.Active)
                || template.Columns.Select(c => c.Id).Distinct().Count() != template.Columns.Count
                || !template.Columns.Select(c=>c.Order).Order().SequenceEqual(Enumerable.Range(0,template.Columns.Count))
                || TenantTemplateService.Validate(template.Columns.OrderBy(c=>c.Order).ToArray(),template.Purpose,template.Ledger) is not null)
                return false;
        }
        var templates = document.Templates.ToDictionary(t => t.Id);
        return document.Mappings.All(m => templates.TryGetValue(m.TemplateId, out var template)
            && m.Entries.All(e => template.Columns.Any(c => c.Id == e.ColumnId && c.StorageKey == e.StorageKey)));
    }
}

public sealed class ConfigurationBackupService(PostgresTransactions transactions, TenantColumnService columnService,
    TenantTemplateService templateService, TenantSettingsService settingsService, PostgresAuditWriter audit)
{
    public Task<ConfigurationBackupDocument?> ExportAsync(VerifiedSession? session, CancellationToken ct = default)
    {
        if (session is null || !session.TenantContextRequired || session.TenantId is not > 0
            || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult<ConfigurationBackupDocument?>(null);

        return transactions.ExecuteAsync<ConfigurationBackupDocument?>(async (db, token) =>
        {
            await using (var configuration = db.CreateCommand("SELECT pg_advisory_xact_lock(746102)"))
                await configuration.ExecuteNonQueryAsync(token);
            await using (var tenant = db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
            {
                tenant.Parameters.AddWithValue(session.TenantId.Value);
                if (await tenant.ExecuteScalarAsync(token) is null) return null;
            }
            await using (var actor = db.CreateCommand("""
                SELECT u.id FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                  AND s.selected_tenant_id=$3 AND s.context_version=$4 AND m.tenant_id=$3
                  AND m.is_active IS TRUE AND m.tenant_role='admin' FOR SHARE OF s,u,m
                """))
            {
                actor.Parameters.AddWithValue(session.Token); actor.Parameters.AddWithValue(session.UserId);
                actor.Parameters.AddWithValue(session.TenantId.Value); actor.Parameters.AddWithValue(session.ContextVersion);
                if (await actor.ExecuteScalarAsync(token) is null) return null;
            }

            return await SnapshotAsync(db,session.TenantId.Value,token);
        },ct);
    }

    private static async Task<ConfigurationBackupDocument> SnapshotAsync(PostgresTransactionContext db, int tenantId, CancellationToken token)
    {
            var settings = new SortedDictionary<string,string>(StringComparer.Ordinal);
            await using (var command = db.CreateCommand("SELECT key,value FROM public.platform_tenant_settings WHERE tenant_id=$1 ORDER BY key"))
            {
                command.Parameters.AddWithValue(tenantId);
                await using var reader = await command.ExecuteReaderAsync(token);
                while (await reader.ReadAsync(token)) settings.Add(reader.GetString(0),reader.GetString(1));
            }
            var columns = new List<TenantColumn>();
            await using (var command = db.CreateCommand("""
                SELECT m.id,c.display_name,m.column_name,m.data_type,c.category,c.notes,m.is_active,c.binding_origin,c.definition_version
                FROM public.platform_tenant_columns c JOIN public.master_columns m ON m.id=c.column_id
                WHERE c.tenant_id=$1 ORDER BY m.id FOR SHARE OF c,m
                """))
            {
                command.Parameters.AddWithValue(tenantId);
                await using var reader = await command.ExecuteReaderAsync(token);
                while (await reader.ReadAsync(token)) columns.Add(new(reader.GetInt32(0),reader.GetString(1),reader.GetString(2),
                    reader.GetString(3),reader.GetString(4),reader.IsDBNull(5)?null:reader.GetString(5),
                    !reader.IsDBNull(6)&&reader.GetBoolean(6),reader.GetString(7),reader.GetInt64(8)));
            }
            await using (var inactive=db.CreateCommand("""
                SELECT EXISTS(SELECT 1 FROM public.platform_tenant_templates d JOIN public.templates t ON t.id=d.template_id
                    WHERE d.tenant_id=$1 AND t.is_active IS NOT TRUE)
                """))
            {
                inactive.Parameters.AddWithValue(tenantId);
                if((bool)(await inactive.ExecuteScalarAsync(token))!)
                    throw new BackupReviewException("Inactive templates require separate backup review.");
            }
            var templates = await TenantTemplateService.LoadAsync(db,tenantId,null,token);
            // A joined read must not silently omit a legacy or broken binding.
            await using (var command=db.CreateCommand("""
                SELECT EXISTS(SELECT 1 FROM public.template_column_mappings p
                    JOIN public.platform_tenant_templates t ON t.template_id=p.template_id
                    JOIN public.templates original ON original.id=t.template_id
                    LEFT JOIN public.master_columns m ON m.id=p.column_id
                    LEFT JOIN public.platform_tenant_columns c ON c.column_id=p.column_id AND c.tenant_id=t.tenant_id
                    WHERE t.tenant_id=$1 AND original.is_active IS TRUE
                      AND (p.tenant_id IS DISTINCT FROM t.tenant_id OR p.storage_key IS DISTINCT FROM m.column_name
                           OR c.binding_origin IS DISTINCT FROM 'native' OR m.is_active IS DISTINCT FROM TRUE))
                """))
            {
                command.Parameters.AddWithValue(tenantId);
                if ((bool)(await command.ExecuteScalarAsync(token))!)
                    throw new BackupReviewException("Template bindings need review before backup.");
            }
            var mappings = new List<BackupMapping>();
            await using (var clients=db.CreateCommand("""
                SELECT EXISTS(SELECT 1 FROM public.platform_tenant_saved_mapping_sets s
                    LEFT JOIN public.platform_tenant_client_homes h ON h.client_id=s.client_id
                    LEFT JOIN public.clients c ON c.id=s.client_id
                    WHERE s.tenant_id=$1 AND (h.tenant_id IS DISTINCT FROM s.tenant_id OR c.is_active IS DISTINCT FROM TRUE))
                """))
            {
                clients.Parameters.AddWithValue(tenantId);
                if((bool)(await clients.ExecuteScalarAsync(token))!)
                    throw new BackupReviewException("Inactive or unverified client mappings require backup review.");
            }
            await using (var command = db.CreateCommand("""
                SELECT s.client_id,s.template_id,s.template_binding_key,s.definition_version,m.column_id,m.storage_key,m.excel_column
                FROM public.platform_tenant_saved_mapping_sets s
                LEFT JOIN public.platform_tenant_saved_mappings m ON m.tenant_id=s.tenant_id
                  AND m.client_id=s.client_id AND m.template_id=s.template_id
                WHERE s.tenant_id=$1 ORDER BY s.client_id,s.template_id,m.column_id
                """))
            {
                command.Parameters.AddWithValue(tenantId);
                await using var reader = await command.ExecuteReaderAsync(token);
                while (await reader.ReadAsync(token))
                {
                    int client=reader.GetInt32(0), template=reader.GetInt32(1);
                    if (mappings.Count==0 || mappings[^1].ClientId!=client || mappings[^1].TemplateId!=template)
                        mappings.Add(new(client,template,reader.GetString(2),reader.GetInt64(3),new List<BackupMappingEntry>()));
                    if (!reader.IsDBNull(4)) ((List<BackupMappingEntry>)mappings[^1].Entries).Add(
                        new(reader.GetInt32(4),reader.GetString(5),reader.GetString(6)));
                }
            }
            var holidays = new List<BackupHoliday>();
            await using (var command=db.CreateCommand("SELECT id,holiday_date,label,repeats_yearly FROM public.platform_tenant_holidays WHERE tenant_id=$1 ORDER BY id"))
            {
                command.Parameters.AddWithValue(tenantId);
                await using var reader=await command.ExecuteReaderAsync(token);
                while(await reader.ReadAsync(token)) holidays.Add(new(reader.GetInt32(0),reader.GetFieldValue<DateOnly>(1),reader.GetString(2),reader.GetBoolean(3)));
            }
            var result = new ConfigurationBackupDocument(1,tenantId,settings,columns,templates,mappings,holidays);
            if (!ConfigurationBackupValidation.IsValid(result,tenantId))
                throw new BackupReviewException("Configuration bindings need review before backup.");
            return result;
    }

    public Task<BackupResult> PreviewAsync(VerifiedSession? session, ConfigurationBackupDocument? backup, CancellationToken ct = default) =>
        ExecuteAsync(session,async (db,actor,token) =>
        {
            if (!ConfigurationBackupValidation.IsValid(backup,actor.Tenant)) return new("invalid","Invalid backup or workspace.");
            var error=await TenantSettingsService.ValidateRestoreAsync(db,actor.Tenant,backup!.Settings,token);
            if(error is not null) return new("invalid",error);
            var current=await SnapshotAsync(db,actor.Tenant,token);
            if (!Compatible(backup!,current)) return new("review","Some backup identities are missing or require review.");
            return new("success", "Restore replaces listed settings and existing configuration, including any upload backup policy in the file. Review before confirming.", Revision(backup!,current));
        },ct);

    public Task<BackupResult> RestoreAsync(VerifiedSession? session, ConfigurationBackupDocument? backup,
        string? revision, bool confirmed, CancellationToken ct = default) =>
        ExecuteAsync(session,async (db,actor,token) =>
        {
            if (!confirmed || revision is null || !ConfigurationBackupValidation.IsValid(backup,actor.Tenant))
                return new("invalid","A valid backup, preview and confirmation are required.");
            var source=backup!;
            var current=await SnapshotAsync(db,actor.Tenant,token);
            if (!Compatible(source,current)) return new("review","Some backup identities are missing or require review.");
            if (revision!=Revision(source,current)) return new("conflict","Configuration changed. Preview again.");

            // Any rejection after the first write throws, so the outer transaction rolls back.
            var settingsError=await settingsService.RestoreAsync(db,actor.Tenant,actor.User,actor.Name,source.Settings,token);
            if (settingsError is not null) throw new BackupRejectedException(settingsError);
            foreach (var column in source.Columns)
            {
                var result=await columnService.RestoreAsync(db,actor.Tenant,actor.User,actor.Name,column.Id,
                    new ColumnInput(column.Name,column.Type,column.Category,column.Notes,column.Active),token);
                if(result.Status!=ColumnStatus.Success) throw new BackupRejectedException(result.Detail??"Column requires review.");
            }
            foreach (var wanted in source.Templates)
            {
                var before=(await TenantTemplateService.LoadAsync(db,actor.Tenant,wanted.Id,token)).Single();
                var old=before.Columns.ToDictionary(c=>c.Id);
                var comparisons=new Dictionary<int,bool>();
                foreach(var column in wanted.Columns)
                {
                    old.TryGetValue(column.Id,out var previous);
                    if(column.Review || column.Compare is null)
                    {
                        if(previous is null || previous.Review!=column.Review || previous.Compare!=column.Compare)
                            throw new BackupRejectedException("Comparison settings require review before restore.");
                    }
                    else comparisons.Add(column.Id,column.Compare.Value);
                }
                var input=new TemplateInput(wanted.Name,wanted.Columns.OrderBy(c=>c.Order).Select(c=>c.Id).ToArray(),
                    wanted.Purpose,wanted.Ledger,wanted.Columns.ToDictionary(c=>c.Id,c=>c.Role),comparisons,
                    wanted.RateSource,wanted.Subtotals,before.ConfigurationKey);
                var result=await templateService.RestoreAsync(db,actor.Tenant,actor.User,actor.Name,wanted.Id,input,token);
                if(result.Status!=TemplateStatus.Success) throw new BackupRejectedException(result.Detail??"Template requires review.");
            }
            foreach(var holiday in source.Holidays)
            {
                await using var command=db.CreateCommand("UPDATE public.platform_tenant_holidays SET holiday_date=$1,label=$2,repeats_yearly=$3 WHERE tenant_id=$4 AND id=$5");
                command.Parameters.AddWithValue(holiday.Date); command.Parameters.AddWithValue(holiday.Label.Trim());
                command.Parameters.AddWithValue(holiday.RepeatsYearly); command.Parameters.AddWithValue(actor.Tenant); command.Parameters.AddWithValue(holiday.Id);
                if(await command.ExecuteNonQueryAsync(token)!=1) throw new BackupRejectedException("Holiday no longer exists.");
            }
            foreach(var mapping in source.Mappings)
            {
                await using(var client=db.CreateCommand("""
                    SELECT c.id FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
                    WHERE c.id=$1 AND h.tenant_id=$2 AND c.is_active IS TRUE FOR SHARE OF c,h
                    """))
                { client.Parameters.AddWithValue(mapping.ClientId); client.Parameters.AddWithValue(actor.Tenant);
                  if(await client.ExecuteScalarAsync(token) is null) throw new BackupRejectedException("Client access requires review."); }
                var template=(await TenantTemplateService.LoadAsync(db,actor.Tenant,mapping.TemplateId,token)).Single();
                if (SavedMappingDefinition.BindingKey(template)!=mapping.BindingKey)
                    throw new BackupRejectedException("Saved mapping bindings require review.");
                await using(var header=db.CreateCommand("UPDATE public.platform_tenant_saved_mapping_sets SET template_binding_key=$1 WHERE tenant_id=$2 AND client_id=$3 AND template_id=$4"))
                { header.Parameters.AddWithValue(mapping.BindingKey); header.Parameters.AddWithValue(actor.Tenant); header.Parameters.AddWithValue(mapping.ClientId); header.Parameters.AddWithValue(mapping.TemplateId);
                  if(await header.ExecuteNonQueryAsync(token)!=1) throw new BackupRejectedException("Saved mapping no longer exists."); }
                await using(var clear=db.CreateCommand("DELETE FROM public.platform_tenant_saved_mappings WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3"))
                { clear.Parameters.AddWithValue(actor.Tenant); clear.Parameters.AddWithValue(mapping.ClientId); clear.Parameters.AddWithValue(mapping.TemplateId); await clear.ExecuteNonQueryAsync(token); }
                if(mapping.Entries.Count>0)
                {
                    await using var insert=db.CreateCommand("""
                        INSERT INTO public.platform_tenant_saved_mappings(tenant_id,client_id,template_id,column_id,storage_key,excel_column)
                        SELECT $1,$2,$3,e.column_id,e.storage_key,e.excel_column
                        FROM unnest($4::integer[],$5::text[],$6::text[]) AS e(column_id,storage_key,excel_column)
                        """);
                    insert.Parameters.AddWithValue(actor.Tenant); insert.Parameters.AddWithValue(mapping.ClientId); insert.Parameters.AddWithValue(mapping.TemplateId);
                    insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Integer,mapping.Entries.Select(e=>e.ColumnId).ToArray());
                    insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,mapping.Entries.Select(e=>e.StorageKey).ToArray());
                    insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,mapping.Entries.Select(e=>e.Header).ToArray());
                    await insert.ExecuteNonQueryAsync(token);
                }
                await using(var plans=db.CreateCommand("UPDATE public.upload_validation_plans SET status='superseded' WHERE client_id=$1 AND template_id=$2 AND status='ready'"))
                { plans.Parameters.AddWithValue(mapping.ClientId); plans.Parameters.AddWithValue(mapping.TemplateId); await plans.ExecuteNonQueryAsync(token); }
                await audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.User,actor.Name),mapping.ClientId,
                    "Restore","TenantMappings",JsonSerializer.SerializeToElement(new {tenant_id=actor.Tenant,template_id=mapping.TemplateId})),token);
            }
            await audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.User,actor.Name),null,
                "Restore","ConfigurationBackup",JsonSerializer.SerializeToElement(new {tenant_id=actor.Tenant})),token);
            return new("success","Configuration restored.");
        },ct);

    private sealed record Actor(int Tenant,int User,string Name);
    private sealed class BackupRejectedException(string message) : Exception(message);
    private async Task<BackupResult> ExecuteAsync(VerifiedSession? session,
        Func<PostgresTransactionContext,Actor,CancellationToken,Task<BackupResult>> action,CancellationToken ct)
    {
        if(session is null || !session.TenantContextRequired || session.TenantId is not >0 || !AuthenticationService.ValidToken(session.Token)) return new("forbidden");
        try
        {
            return await transactions.ExecuteAsync(async(db,token)=>
            {
                await using(var config=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await config.ExecuteNonQueryAsync(token);
                await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
                { tenant.Parameters.AddWithValue(session.TenantId.Value); if(await tenant.ExecuteScalarAsync(token) is null) return new BackupResult("forbidden"); }
                Actor actor;
                await using(var command=db.CreateCommand("""
                    SELECT u.id,u.username FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                    JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                    WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE AND s.selected_tenant_id=$3
                      AND s.context_version=$4 AND m.tenant_id=$3 AND m.is_active IS TRUE AND m.tenant_role='admin' FOR SHARE OF s,u,m
                    """))
                { command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
                  command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
                  await using var reader=await command.ExecuteReaderAsync(token); if(!await reader.ReadAsync(token)) return new BackupResult("forbidden");
                  actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1)); }
                return await action(db,actor,token);
            },ct);
        }
        catch(BackupRejectedException error) { return new("review",error.Message); }
        catch(BackupReviewException error) { return new("review",error.Message); }
        catch(PostgresException error) when(error.SqlState is PostgresErrorCodes.UniqueViolation
            or PostgresErrorCodes.ForeignKeyViolation or PostgresErrorCodes.CheckViolation or PostgresErrorCodes.NotNullViolation)
        { return new("review","Configuration conflicts with an existing definition or binding."); }
    }

    private static string Revision(ConfigurationBackupDocument backup,ConfigurationBackupDocument current) =>
        Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(new {backup,current})));

    private static bool Compatible(ConfigurationBackupDocument backup,ConfigurationBackupDocument current)
    {
        var columns=current.Columns.ToDictionary(c=>c.Id);
        var templates=current.Templates.ToDictionary(t=>t.Id);
        var backupTemplates=backup.Templates.ToDictionary(t=>t.Id);
        var mappings=current.Mappings.Select(m=>(m.ClientId,m.TemplateId)).ToHashSet();
        var holidays=current.Holidays.Select(h=>h.Id).ToHashSet();
        var columnIds=backup.Columns.Select(c=>c.Id).ToHashSet();
        var templateIds=backup.Templates.Select(t=>t.Id).ToHashSet();
        var columnNames=backup.Columns.Select(c=>c.Name).Concat(current.Columns.Where(c=>!columnIds.Contains(c.Id)).Select(c=>c.Name)).ToArray();
        var templateNames=backup.Templates.Select(t=>t.Name.Trim()).Concat(current.Templates.Where(t=>!templateIds.Contains(t.Id)).Select(t=>t.Name)).ToArray();
        return backup.Columns.All(c=>columns.TryGetValue(c.Id,out var existing) && existing.Origin=="native" && existing.StorageKey==c.StorageKey)
            && backup.Templates.All(t=>templates.TryGetValue(t.Id,out var existing) && existing.Origin=="native" && existing.Kind==t.Kind)
            && columnNames.Distinct(StringComparer.OrdinalIgnoreCase).Count()==columnNames.Length
            && templateNames.Distinct(StringComparer.OrdinalIgnoreCase).Count()==templateNames.Length
            && backup.Mappings.All(m=>mappings.Contains((m.ClientId,m.TemplateId))
                && backupTemplates.TryGetValue(m.TemplateId,out var definition)
                && m.BindingKey==SavedMappingDefinition.BindingKey(definition))
            && backup.Holidays.All(h=>holidays.Contains(h.Id));
    }

}
