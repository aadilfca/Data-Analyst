using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Columns;

public enum ColumnStatus { Success, Forbidden, NotFound, Invalid, Conflict, DependencyReview }
public sealed record ColumnInput(string Name,string Type,string Category="General",string? Notes=null,
    bool Active=true,string? ReconciliationRole=null);
public sealed record TenantColumn(int Id,string Name,string StorageKey,string Type,string Category,
    string? Notes,bool Active,string Origin,long Version);
public sealed record ColumnResult(ColumnStatus Status,IReadOnlyList<TenantColumn>? Columns=null,int? ColumnId=null,string? Detail=null,int TemplatesCount=0);

// The master row carries the permanent ID/storage key/base type. Tenant-facing
// labels and annotations are separate, so label edits never rename stored JSON.
public sealed class TenantColumnService(PostgresTransactions transactions,PostgresAuditWriter audit,IOptions<TemplateManagementOptions>? templates=null)
{
    private sealed record Actor(int TenantId,int UserId,string Name);
    private const string Fields="m.id,c.display_name,m.column_name,m.data_type,c.category,c.notes,m.is_active,c.binding_origin,c.definition_version";
    private const string RoleError="Select Document ID, Document Date or Footing in Edit Template; shared columns keep their base type.";
    private const string TypeError="Choose Text, Numeric or Date. Document ID, Document Date and Footing are selected in Edit Template.";
    private const string ReviewError="Column dependencies or legacy data bindings need review before this change";

    public Task<ColumnResult> ListAsync(VerifiedSession? session,bool all,CancellationToken ct=default) =>
        RunAsync(session,false,async(db,actor,token)=>
        {
            await using var command=db.CreateCommand($"""
                SELECT {Fields} FROM public.platform_tenant_columns c JOIN public.master_columns m ON m.id=c.column_id
                WHERE c.tenant_id=$1 AND ($2 OR m.is_active IS TRUE) ORDER BY c.category,c.display_name,m.id
                """);
            command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(all);
            await using var reader=await command.ExecuteReaderAsync(token);
            var rows=new List<TenantColumn>(); while(await reader.ReadAsync(token)) rows.Add(Read(reader));
            return new(ColumnStatus.Success,rows);
        },ct);

    public async Task<ColumnResult> SaveAsync(VerifiedSession? session,int? id,ColumnInput input,CancellationToken ct=default)
    {
        try
        {
            return await RunAsync(session,true,async(db,actor,token)=>
            {
                var existing=id is null ? null : await TargetAsync(db,actor.TenantId,id.Value,token);
                if(id is not null && existing is null) return new(ColumnStatus.NotFound);
                if(input.ReconciliationRole is not null) return new(ColumnStatus.Invalid,Detail:RoleError);
                if(input.Type is not ("Text" or "Numeric" or "Date")) return new(ColumnStatus.Invalid,Detail:TypeError);
                if(string.IsNullOrWhiteSpace(input.Name)) return new(ColumnStatus.Invalid,Detail:"Column name is required");
                if(existing is null)
                {
                    int created;
                    // The globally unique legacy field now stores an opaque,
                    // server-generated key for NEW native definitions only.
                    // No legacy row, name, ID, transaction or fingerprint changes.
                    await using(var insert=db.CreateCommand("""
                        INSERT INTO public.master_columns(column_name,data_type,category,notes)
                        VALUES($1,$2,'General',NULL) RETURNING id
                        """))
                    {
                        insert.Parameters.AddWithValue("col_"+Guid.NewGuid().ToString("N")); insert.Parameters.AddWithValue(input.Type);
                        created=(int)(await insert.ExecuteScalarAsync(token))!;
                    }
                    await using(var definition=db.CreateCommand("""
                        INSERT INTO public.platform_tenant_columns(column_id,tenant_id,display_name,category,notes,binding_origin)
                        VALUES($1,$2,$3,$4,$5,'native')
                        """))
                    {
                        definition.Parameters.AddWithValue(created); definition.Parameters.AddWithValue(actor.TenantId);
                        definition.Parameters.AddWithValue(input.Name); definition.Parameters.AddWithValue(input.Category);
                        definition.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)input.Notes??DBNull.Value);
                        await definition.ExecuteNonQueryAsync(token);
                    }
                    await AuditAsync(db,actor,created,"Create",token);
                    return new(ColumnStatus.Success,ColumnId:created);
                }

                bool label=existing.Name!=input.Name;
                bool structural=existing.Type!=input.Type || existing.Active!=input.Active;
                // P13 verifies native template ID/key consumers. Every other
                // dependency, and every structural change, remains guarded.
                if((label||structural) && (existing.Origin!="native" || await HasDependenciesAsync(db,existing,token,!structural,actor.TenantId)))
                    return new(ColumnStatus.DependencyReview,Detail:ReviewError);
                if(structural && await HasStoredDataAsync(db,existing.StorageKey,token))
                    return new(ColumnStatus.DependencyReview,Detail:ReviewError);
                if(structural)
                {
                    await using var update=db.CreateCommand("UPDATE public.master_columns SET data_type=$1,is_active=$2 WHERE id=$3");
                    update.Parameters.AddWithValue(input.Type); update.Parameters.AddWithValue(input.Active); update.Parameters.AddWithValue(existing.Id);
                    await update.ExecuteNonQueryAsync(token);
                }
                // These tenant-only annotations never modify the legacy master
                // name/category/notes or free-text audit-rule references.
                await using(var update=db.CreateCommand("""
                    UPDATE public.platform_tenant_columns SET display_name=$1,category=$2,notes=$3
                    WHERE tenant_id=$4 AND column_id=$5
                    """))
                {
                    update.Parameters.AddWithValue(input.Name); update.Parameters.AddWithValue(input.Category);
                    update.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)input.Notes??DBNull.Value);
                    update.Parameters.AddWithValue(actor.TenantId); update.Parameters.AddWithValue(existing.Id);
                    await update.ExecuteNonQueryAsync(token);
                }
                if(templates?.Value.Enabled==true)
                {
                    await using var plans=db.CreateCommand("""
                        UPDATE public.upload_validation_plans SET status='superseded' WHERE status='ready'
                        AND template_id IN(SELECT template_id FROM public.template_column_mappings WHERE tenant_id=$1 AND column_id=$2)
                        """);
                    plans.Parameters.AddWithValue(actor.TenantId); plans.Parameters.AddWithValue(existing.Id); await plans.ExecuteNonQueryAsync(token);
                }
                await AuditAsync(db,actor,existing.Id,"Update",token);
                return new(ColumnStatus.Success,ColumnId:existing.Id);
            },ct);
        }
        catch(PostgresException error) when(error.SqlState==PostgresErrorCodes.UniqueViolation)
        { return new(ColumnStatus.Conflict,Detail:"Column name already exists in this workspace"); }
    }

    public Task<ColumnResult> DeleteAsync(VerifiedSession? session,int id,CancellationToken ct=default) =>
        RunAsync(session,true,async(db,actor,token)=>
        {
            var existing=await TargetAsync(db,actor.TenantId,id,token);
            if(existing is null || !existing.Active) return new(ColumnStatus.NotFound);
            if(existing.Origin!="native" || await HasDependenciesAsync(db,existing,token)
                || await HasStoredDataAsync(db,existing.StorageKey,token))
                return new(ColumnStatus.DependencyReview,Detail:ReviewError);
            await using var command=db.CreateCommand("UPDATE public.master_columns SET is_active=FALSE WHERE id=$1");
            command.Parameters.AddWithValue(id); await command.ExecuteNonQueryAsync(token);
            await AuditAsync(db,actor,id,"Delete",token);
            return new(ColumnStatus.Success,ColumnId:id);
        },ct);

    public Task<ColumnResult> UsageAsync(VerifiedSession? session,int id,CancellationToken ct=default) =>
        RunAsync(session,false,async(db,actor,token)=>
        {
            var column=await TargetAsync(db,actor.TenantId,id,token);
            if(column is null) return new(ColumnStatus.NotFound);
            // Until dependent modules have verified tenant ownership, positive
            // global counts would disclose unscoped information. Never report
            // zero merely because a consumer has not been ported.
            if(column.Origin!="native" || await HasDependenciesAsync(db,column,token,true,actor.TenantId)
                || await HasStoredDataAsync(db,column.StorageKey,token))
                return new(ColumnStatus.DependencyReview,Detail:ReviewError);
            int count=0;
            if(templates?.Value.Enabled==true)
            {
                await using var usage=db.CreateCommand("""
                    SELECT count(*)::integer FROM public.template_column_mappings p JOIN public.templates t ON t.id=p.template_id
                    WHERE p.column_id=$1 AND p.tenant_id=$2 AND t.is_active IS TRUE
                    """);
                usage.Parameters.AddWithValue(id); usage.Parameters.AddWithValue(actor.TenantId); count=(int)(await usage.ExecuteScalarAsync(token))!;
            }
            return new(ColumnStatus.Success,TemplatesCount:count);
        },ct);

    private Task<ColumnResult> RunAsync(VerifiedSession? session,bool admin,
        Func<PostgresTransactionContext,Actor,CancellationToken,Task<ColumnResult>> operation,CancellationToken ct)
    {
        if(session is null || !session.TenantContextRequired || session.TenantId is not >0 || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult(new ColumnResult(ColumnStatus.Forbidden));
        return transactions.ExecuteAsync(async(db,token)=>
        {
            // Share legacy column/template serialization before any metadata
            // row locks. Future dependent writers must use the same contract.
            await using(var configuration=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await configuration.ExecuteNonQueryAsync(token);
            await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
            {
                tenant.Parameters.AddWithValue(session.TenantId.Value);
                if(await tenant.ExecuteScalarAsync(token) is null) return new ColumnResult(ColumnStatus.Forbidden);
            }
            Actor actor;
            await using(var command=db.CreateCommand("""
                SELECT u.id,u.username,m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                  AND s.selected_tenant_id=$3 AND s.context_version=$4
                  AND m.tenant_id=$3 AND m.is_active IS TRUE FOR SHARE OF s,u,m
                """))
            {
                command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
                command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
                await using var reader=await command.ExecuteReaderAsync(token);
                if(!await reader.ReadAsync(token) || (admin && reader.GetString(2)!="admin")) return new ColumnResult(ColumnStatus.Forbidden);
                actor=new(session.TenantId.Value,reader.GetInt32(0),reader.GetString(1));
            }
            return await operation(db,actor,token);
        },ct);
    }

    private static async Task<TenantColumn?> TargetAsync(PostgresTransactionContext db,int tenant,int id,CancellationToken ct)
    {
        await using var command=db.CreateCommand($"""
            SELECT {Fields} FROM public.platform_tenant_columns c JOIN public.master_columns m ON m.id=c.column_id
            WHERE c.tenant_id=$1 AND m.id=$2 FOR UPDATE OF c,m
            """);
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(id);
        await using var reader=await command.ExecuteReaderAsync(ct); return await reader.ReadAsync(ct) ? Read(reader) : null;
    }
    private static TenantColumn Read(NpgsqlDataReader r) => new(r.GetInt32(0),r.GetString(1),r.GetString(2),r.GetString(3),
        r.GetString(4),r.IsDBNull(5)?null:r.GetString(5),!r.IsDBNull(6)&&r.GetBoolean(6),r.GetString(7),r.GetInt64(8));

    private async Task<bool> HasDependenciesAsync(PostgresTransactionContext db,TenantColumn column,CancellationToken ct,bool allowTemplates=false,int tenant=0)
    {
        // Missing/unreviewed dependent schemas fail the operation instead of
        // treating missing tables as proof that a column is unused.
        var templateDependency=allowTemplates&&templates?.Value.Enabled==true ? """
            EXISTS(SELECT 1 FROM public.template_column_mappings p WHERE p.column_id=$1 AND NOT EXISTS(
                SELECT 1 FROM public.platform_tenant_templates d WHERE d.template_id=p.template_id
                AND d.tenant_id=p.tenant_id AND d.binding_origin='native' AND p.tenant_id=$3 AND p.storage_key=$2))
            """ : "EXISTS(SELECT 1 FROM public.template_column_mappings WHERE column_id=$1)";
        await using var command=db.CreateCommand($"""
            SELECT {templateDependency}
                OR EXISTS(SELECT 1 FROM public.audit_rule_document_fields WHERE master_column_id=$1
                    OR lower(btrim(field_name))=lower(btrim($2)))
                OR EXISTS(SELECT 1 FROM public.audit_rule_cross_checks
                    WHERE lower(btrim(compare_field))=lower(btrim($2)))
                OR EXISTS(SELECT 1 FROM public.saved_column_mappings WHERE template_column_id=$1)
            """);
        command.Parameters.AddWithValue(column.Id); command.Parameters.AddWithValue(column.StorageKey);
        if(allowTemplates&&templates?.Value.Enabled==true) command.Parameters.AddWithValue(tenant);
        return (bool)(await command.ExecuteScalarAsync(ct))!;
    }
    private Task<int> AuditAsync(PostgresTransactionContext db,Actor actor,int id,string action,CancellationToken ct) =>
        audit.WriteAsync(db,new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),new LogActor("authenticated",actor.UserId,actor.Name),null,
            action,"TenantColumns",JsonSerializer.SerializeToElement(new {tenant_id=actor.TenantId,record_id=id})),ct);

    private static async Task<bool> HasStoredDataAsync(PostgresTransactionContext db,string key,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT EXISTS(SELECT 1 FROM public.client_data WHERE row_data ? $1)
                OR EXISTS(SELECT 1 FROM public.client_data_history WHERE row_data ? $1)
            """);
        command.Parameters.AddWithValue(key); return (bool)(await command.ExecuteScalarAsync(ct))!;
    }
}
