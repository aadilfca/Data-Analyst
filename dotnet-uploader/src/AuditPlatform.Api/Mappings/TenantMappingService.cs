using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Templates;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Mappings;

public sealed class TenantMappingService(PostgresTransactions transactions,PostgresAuditWriter audit)
{
    // Once native bindings exist, switching the HTTP feature off must not
    // disable their dependency safeguards in older module APIs.
    internal static async Task<bool> StoreExistsAsync(PostgresTransactionContext db,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT to_regclass('public.platform_tenant_saved_mappings') IS NOT NULL");
        return (bool)(await command.ExecuteScalarAsync(ct))!;
    }
    private sealed record Actor(int Tenant,int User,string Name);
    private sealed record Stored(long Revision,string? Binding,IReadOnlyList<SavedHeader> Entries,bool PendingAdoption);
    private const string StaleMessage="Saved mapping or template changed. Reload and review the mapping before saving.";

    public Task<MappingResult> ReadAsync(VerifiedSession? session,int client,int template,CancellationToken ct=default)=>
        RunAsync(session,client,template,async(db,actor,definition,token)=>
            new(MappingStatus.Success,Snapshot(definition,await StoredAsync(db,actor.Tenant,client,template,token))),ct);

    public Task<MappingResult> SaveAsync(VerifiedSession? session,int client,int template,IReadOnlyList<MappingEntry> entries,
        long revision,string configuration,CancellationToken ct=default)=>WriteAsync(session,client,template,entries,revision,configuration,false,ct);
    public Task<MappingResult> ClearAsync(VerifiedSession? session,int client,int template,long revision,string configuration,CancellationToken ct=default)=>
        WriteAsync(session,client,template,[],revision,configuration,true,ct);

    private Task<MappingResult> WriteAsync(VerifiedSession? session,int client,int template,IReadOnlyList<MappingEntry> entries,
        long revision,string configuration,bool clear,CancellationToken ct)=>RunAsync(session,client,template,async(db,actor,definition,token)=>
        {
            var before=await StoredAsync(db,actor.Tenant,client,template,token);
            if(revision<0 || revision!=before.Revision || configuration!=definition.ConfigurationKey)
                return new(MappingStatus.Conflict,Detail:StaleMessage);
            var columns=definition.Columns.ToDictionary(c=>c.Id);
            if(entries.Count>columns.Count || entries.Any(e=>e is null||e.Header is null||e.Header.Contains('\0'))
                || entries.Select(e=>e.ColumnId).Distinct().Count()!=entries.Count || entries.Any(e=>!columns.ContainsKey(e.ColumnId)))
                return new(MappingStatus.Invalid,Detail:"Select distinct active columns belonging to this template.");
            // Blank choices are omitted, exactly as in the legacy full-replace
            // API. Nonblank header spelling/spacing is retained literally.
            var saved=entries.Where(e=>!string.IsNullOrWhiteSpace(e.Header)).ToArray();
            await using(var header=db.CreateCommand("""
                INSERT INTO public.platform_tenant_saved_mapping_sets(tenant_id,client_id,template_id,template_binding_key)
                VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,client_id,template_id)
                DO UPDATE SET template_binding_key=EXCLUDED.template_binding_key
                """))
            {
                AddScope(header,actor.Tenant,client,template); header.Parameters.AddWithValue(SavedMappingDefinition.BindingKey(definition));
                await header.ExecuteNonQueryAsync(token);
            }
            await using(var remove=db.CreateCommand("DELETE FROM public.platform_tenant_saved_mappings WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3"))
            { AddScope(remove,actor.Tenant,client,template); await remove.ExecuteNonQueryAsync(token); }
            if(saved.Length>0)
            {
                // One bounded parameterized bulk statement, never one remote
                // insert per column. No workbook cells or comparison overrides.
                await using var insert=db.CreateCommand("""
                    INSERT INTO public.platform_tenant_saved_mappings(tenant_id,client_id,template_id,column_id,storage_key,excel_column)
                    SELECT $1,$2,$3,u.column_id,u.storage_key,u.excel_column
                    FROM unnest($4::integer[],$5::text[],$6::text[]) AS u(column_id,storage_key,excel_column)
                    """);
                AddScope(insert,actor.Tenant,client,template);
                insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Integer,saved.Select(e=>e.ColumnId).ToArray());
                insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,saved.Select(e=>columns[e.ColumnId].StorageKey).ToArray());
                insert.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,saved.Select(e=>e.Header).ToArray());
                await insert.ExecuteNonQueryAsync(token);
            }
            await using(var plans=db.CreateCommand("UPDATE public.upload_validation_plans SET status='superseded' WHERE client_id=$1 AND template_id=$2 AND status='ready'"))
            { plans.Parameters.AddWithValue(client); plans.Parameters.AddWithValue(template); await plans.ExecuteNonQueryAsync(token); }
            await audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.User,actor.Name),client,
                clear ? "Delete":"Save","TenantMappings",JsonSerializer.SerializeToElement(new {tenant_id=actor.Tenant,template_id=template})),token);
            if(await ReviewStoreExistsAsync(db,token))
            {
                await using var resolved=db.CreateCommand("DELETE FROM public.platform_saved_mapping_adoption_review WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3");
                AddScope(resolved,actor.Tenant,client,template);await resolved.ExecuteNonQueryAsync(token);
            }
            var after=Snapshot(definition,await StoredAsync(db,actor.Tenant,client,template,token));
            return new(MappingStatus.Success,after,clear ? before.Entries.Count:saved.Length);
        },ct);

    private Task<MappingResult> RunAsync(VerifiedSession? session,int client,int template,
        Func<PostgresTransactionContext,Actor,TemplateDefinition,CancellationToken,Task<MappingResult>> operation,CancellationToken ct)
    {
        if(session is null || !session.TenantContextRequired || session.TenantId is not >0 || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult(new MappingResult(MappingStatus.Forbidden));
        return transactions.ExecuteAsync(async(db,token)=>
        {
            // Membership/client mutations take this same lock, so in-app
            // revocation cannot race this authorization and mapping write.
            await using(var config=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await config.ExecuteNonQueryAsync(token);
            await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE"))
            { tenant.Parameters.AddWithValue(session.TenantId.Value); if(await tenant.ExecuteScalarAsync(token) is null) return new MappingResult(MappingStatus.Forbidden); }
            Actor actor; string role;
            await using(var command=db.CreateCommand("""
                SELECT u.id,u.username,m.tenant_role,s.selected_client_id FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                    AND s.selected_tenant_id=$3 AND s.context_version=$4 AND m.tenant_id=$3 AND m.is_active IS TRUE FOR SHARE OF s,u
                """))
            {
                command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
                command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
                await using var reader=await command.ExecuteReaderAsync(token);
                if(!await reader.ReadAsync(token)||reader.IsDBNull(3)||reader.GetInt32(3)!=client) return new MappingResult(MappingStatus.Forbidden);
                actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1)); role=reader.GetString(2);
            }
            await using(var home=db.CreateCommand("""
                SELECT c.id FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
                WHERE c.id=$1 AND h.tenant_id=$2 AND c.is_active IS TRUE
                """))
            { home.Parameters.AddWithValue(client); home.Parameters.AddWithValue(actor.Tenant); if(await home.ExecuteScalarAsync(token) is null) return new MappingResult(MappingStatus.NotFound); }
            if(role!="admin")
            {
                // This is the P14 action grant, not an inherited upload/report
                // permission. Assignment changes outside this app affect later
                // requests; an already-authorized transaction may finish.
                await using var assignment=db.CreateCommand("""
                    SELECT a.user_id FROM public.platform_engagement_assignments a
                    JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                    WHERE a.firm_tenant_id=$1 AND a.user_id=$2 AND e.client_id=$3
                        AND a.team_role IN ('manager','internee') AND a.is_active IS TRUE AND e.is_active IS TRUE
                    """);
                assignment.Parameters.AddWithValue(actor.Tenant); assignment.Parameters.AddWithValue(actor.User); assignment.Parameters.AddWithValue(client);
                if(await assignment.ExecuteScalarAsync(token) is null) return new MappingResult(MappingStatus.Forbidden);
            }
            var definition=(await TenantTemplateService.LoadAsync(db,actor.Tenant,template,token,lockRows:false)).SingleOrDefault();
            if(definition is null) return new MappingResult(MappingStatus.NotFound);
            if(definition.Origin!="native" || definition.Columns.Count==0 || definition.Columns.Any(c=>!c.Active))
                return new MappingResult(MappingStatus.Review,Detail:"Template column bindings need migration review.");
            await using(var mappings=db.CreateCommand("""
                SELECT count(*),count(*) FILTER(WHERE p.tenant_id=$2 AND p.storage_key=m.column_name
                    AND c.binding_origin='native' AND m.is_active IS TRUE)
                FROM public.template_column_mappings p LEFT JOIN public.master_columns m ON m.id=p.column_id
                LEFT JOIN public.platform_tenant_columns c ON c.column_id=p.column_id AND c.tenant_id=p.tenant_id
                WHERE p.template_id=$1
                """))
            {
                mappings.Parameters.AddWithValue(template); mappings.Parameters.AddWithValue(actor.Tenant);
                await using var reader=await mappings.ExecuteReaderAsync(token); await reader.ReadAsync(token);
                if(reader.GetInt64(0)!=definition.Columns.Count || reader.GetInt64(1)!=definition.Columns.Count)
                    return new MappingResult(MappingStatus.Review,Detail:"Template column bindings need migration review.");
            }
            return await operation(db,actor,definition,token);
        },ct);
    }

    private static MappingSnapshot Snapshot(TemplateDefinition template,Stored stored)
    {
        string binding=SavedMappingDefinition.BindingKey(template);
        bool review=SavedMappingDefinition.NeedsReview(template,stored.Binding,stored.Entries,stored.PendingAdoption);
        return new(stored.Revision,template.ConfigurationKey,binding,review,stored.Entries);
    }
    private static async Task<Stored> StoredAsync(PostgresTransactionContext db,int tenant,int client,int template,CancellationToken ct)
    {
        long revision=0; string? binding=null;
        await using(var header=db.CreateCommand("""
            SELECT definition_version,template_binding_key FROM public.platform_tenant_saved_mapping_sets
            WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3
            """))
        {
            AddScope(header,tenant,client,template); await using var reader=await header.ExecuteReaderAsync(ct);
            if(await reader.ReadAsync(ct)) { revision=reader.GetInt64(0); binding=reader.GetString(1); }
        }
        var entries=new List<SavedHeader>();
        await using(var command=db.CreateCommand("""
            SELECT column_id,excel_column,storage_key FROM public.platform_tenant_saved_mappings
            WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3 ORDER BY column_id
            """))
        {
            AddScope(command,tenant,client,template); await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct)) entries.Add(new(reader.GetInt32(0),reader.GetString(1),reader.GetString(2)));
        }
        bool pending=false;
        if(await ReviewStoreExistsAsync(db,ct))
        {
            await using var review=db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.platform_saved_mapping_adoption_review WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3)");
            AddScope(review,tenant,client,template);pending=await review.ExecuteScalarAsync(ct) is true;
        }
        return new(revision,binding,entries,pending);
    }
    private static async Task<bool> ReviewStoreExistsAsync(PostgresTransactionContext db,CancellationToken ct)
    {
        await using var command=db.CreateCommand("SELECT to_regclass('public.platform_saved_mapping_adoption_review') IS NOT NULL");
        return await command.ExecuteScalarAsync(ct) is true;
    }
    private static void AddScope(NpgsqlCommand command,int tenant,int client,int template)
    { command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(client); command.Parameters.AddWithValue(template); }
}
