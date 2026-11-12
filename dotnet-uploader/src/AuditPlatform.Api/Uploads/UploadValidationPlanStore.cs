using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using Microsoft.Extensions.Options;
using NpgsqlTypes;
namespace AuditPlatform.Api.Uploads;

public sealed class UploadValidationPlanStore(PostgresTransactions transactions,IOptions<UploadValidationPlanOptions> options,TimeProvider? clock=null,IConfiguration? configuration=null)
{
    private DateTimeOffset Now=>(clock??TimeProvider.System).GetUtcNow();
    private async Task<T> Authorized<T>(VerifiedSession session,int file,CorrectionRequest review,
        Func<PostgresTransactionContext,DateTimeOffset,CancellationToken,Task<T>> action,CancellationToken ct)
    {
        return await transactions.ExecuteAsync((db,token)=>AuthorizeAsync(db,session,file,review,true,action,token),ct);
    }

    // Imports borrow their one outer transaction; recovery checks access without
    // requiring an unconsumed plan or a still-present temporary source.
    internal async Task<T> AuthorizeAsync<T>(PostgresTransactionContext db,VerifiedSession session,int file,CorrectionRequest review,bool requireSource,
        Func<PostgresTransactionContext,DateTimeOffset,CancellationToken,Task<T>> action,CancellationToken token)
    {
        if(!session.TenantContextRequired||session.TenantId is not >0||!AuthenticationService.ValidToken(session.Token)) throw new ValidationPlanRejected("plan_access_denied");
        var source=review.Coverage.Footing.Validation.Source;
        // Match existing configuration writers, then the Python import scope lock.
        await using(var config=db.CreateCommand("SELECT pg_advisory_xact_lock(746102)")) await config.ExecuteNonQueryAsync(token);
        await using(var scope=db.CreateCommand("SELECT pg_advisory_xact_lock(hashtext($1))"))
        {scope.Parameters.AddWithValue($"upload-validation:{source.ClientId}:{source.TemplateId}");await scope.ExecuteNonQueryAsync(token);}
        await using(var tenant=db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE"))
        {tenant.Parameters.AddWithValue(session.TenantId.Value);if(await tenant.ExecuteScalarAsync(token) is null) throw new ValidationPlanRejected("plan_access_denied");}
        string role;
        await using(var access=db.CreateCommand("""
            SELECT m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3
            JOIN public.clients c ON c.id=s.selected_client_id
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$3
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE AND m.is_active IS TRUE
              AND s.selected_tenant_id=$3 AND s.context_version=$4 AND s.selected_client_id=$5 AND c.is_active IS TRUE
            FOR SHARE OF s,u,m,c,h
            """))
        {
            access.Parameters.AddWithValue(session.Token);access.Parameters.AddWithValue(session.UserId);access.Parameters.AddWithValue(session.TenantId.Value);
            access.Parameters.AddWithValue(session.ContextVersion);access.Parameters.AddWithValue(source.ClientId);
            role=(string?)await access.ExecuteScalarAsync(token)??throw new ValidationPlanRejected("plan_access_denied");
        }
        if(role!="admin")
        {
            await using var assignment=db.CreateCommand("""
                SELECT a.user_id FROM public.platform_engagement_assignments a JOIN public.platform_audit_engagements e
                ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                WHERE a.firm_tenant_id=$1 AND a.user_id=$2 AND e.client_id=$3 AND a.is_active IS TRUE AND e.is_active IS TRUE
                  AND a.team_role IN('manager','internee') FOR SHARE OF a,e
                """);
            assignment.Parameters.AddWithValue(session.TenantId.Value);assignment.Parameters.AddWithValue(session.UserId);assignment.Parameters.AddWithValue(source.ClientId);
            if(await assignment.ExecuteScalarAsync(token) is null) throw new ValidationPlanRejected("plan_access_denied");
        }
        if(!requireSource) return await action(db,DateTimeOffset.MaxValue,token);
        var definition=(await TenantTemplateService.LoadAsync(db,session.TenantId.Value,source.TemplateId,token)).SingleOrDefault();
        if(definition is null||definition.Origin!="native"||definition.Columns.Any(c=>!c.Active||c.Review)||definition.ConfigurationKey!=source.ConfigurationKey)
            throw new ValidationPlanRejected("plan_configuration_changed");
        await using(var revision=db.CreateCommand("""
            SELECT definition_version FROM public.platform_tenant_saved_mapping_sets
            WHERE tenant_id=$1 AND client_id=$2 AND template_id=$3 FOR SHARE
            """))
        {
            revision.Parameters.AddWithValue(session.TenantId.Value);revision.Parameters.AddWithValue(source.ClientId);revision.Parameters.AddWithValue(source.TemplateId);
            long current=Convert.ToInt64(await revision.ExecuteScalarAsync(token)??0,CultureInfo.InvariantCulture);
            if(current!=source.MappingRevision) throw new ValidationPlanRejected("plan_mapping_changed");
        }
        DateTimeOffset expiry;
        await using(var staged=db.CreateCommand("""
            SELECT s.expires_at,s.sha256 FROM public.platform_upload_staging s JOIN public.uploaded_files f ON f.id=s.upload_id
            WHERE s.upload_id=$1 AND s.tenant_id=$2 AND s.user_id=$3 AND s.client_id=$4 AND s.template_id=$5 AND s.state='ready'
              AND f.client_id=$4 AND f.template_id=$5 AND f.status IN('Pending','Validated') FOR SHARE OF s,f
            """))
        {
            staged.Parameters.AddWithValue(file);staged.Parameters.AddWithValue(session.TenantId.Value);staged.Parameters.AddWithValue(session.UserId);
            staged.Parameters.AddWithValue(source.ClientId);staged.Parameters.AddWithValue(source.TemplateId);
            await using var reader=await staged.ExecuteReaderAsync(token);
            if(!await reader.ReadAsync(token)) throw new ValidationPlanRejected("plan_source_unavailable");
            expiry=new(reader.GetDateTime(0));
            if(expiry<=Now||reader.GetString(1)!=source.Sha256) throw new ValidationPlanRejected("plan_source_changed");
        }
        return await action(db,expiry,token);
    }

    public Task<StoredValidationPlan> FindAsync(VerifiedSession session,int file,CorrectionRequest review,string planId,CancellationToken ct)=>
        Authorized(session,file,review,async(db,_,token)=>await Read(db,session,null,planId,token)??throw new ValidationPlanRejected("plan_not_found"),ct);

    internal async Task<StoredValidationPlan?> Read(PostgresTransactionContext db,VerifiedSession actor,Guid? request,string? id,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT n.envelope,n.signature,CASE WHEN octet_length(p.plan_json->>'payload')<=$5 THEN p.plan_json->>'payload' END,p.status,p.file_id,p.client_id,p.template_id,
                p.file_sha256,p.request_signature,p.expires_at,p.plan_json->>'format'
            FROM public.platform_upload_validation_plans n LEFT JOIN public.upload_validation_plans p ON p.plan_id=n.plan_id
            WHERE n.tenant_id=$1 AND n.user_id=$2 AND (($3::uuid IS NOT NULL AND n.request_id=$3) OR ($4::text IS NOT NULL AND n.plan_id=$4))
            FOR UPDATE OF n
            """);
        command.Parameters.AddWithValue(actor.TenantId!.Value);command.Parameters.AddWithValue(actor.UserId);
        command.Parameters.AddWithValue(NpgsqlDbType.Uuid,(object?)request??DBNull.Value);command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)id??DBNull.Value);
        command.Parameters.AddWithValue(options.Value.MaximumPayloadBytes);
        await using var reader=await command.ExecuteReaderAsync(ct);if(!await reader.ReadAsync(ct)) return null;
        var envelope=JsonSerializer.Deserialize<ValidationPlanEnvelope>(reader.GetString(0))??throw new ValidationPlanRejected("plan_envelope_invalid");
        if(envelope.Tenant!=actor.TenantId||envelope.User!=actor.UserId||request is {} expectedRequest&&envelope.RequestId!=expectedRequest
            ||id is {} expectedId&&envelope.PlanId!=expectedId) throw new ValidationPlanRejected("plan_identity_changed");
        string state=envelope.NoOp?"no_changes":reader.IsDBNull(3)?"unavailable":reader.GetString(3);
        if(!envelope.NoOp&&!reader.IsDBNull(3)&&(reader.GetInt32(4)!=envelope.File||reader.GetInt32(5)!=envelope.Client||reader.GetInt32(6)!=envelope.Template
            ||reader.GetString(7)!=envelope.FileHash||reader.GetString(8)!=envelope.RequestHash
            ||new DateTimeOffset(reader.GetDateTime(9)).UtcTicks/10!=envelope.ExpiresAt.UtcTicks/10
            ||reader.IsDBNull(10)||reader.GetString(10)!=UploadValidationPlanSigner.Format)) state="changed";
        return new(envelope,reader.GetString(1),reader.IsDBNull(2)?"":reader.GetString(2),state);
    }

    internal Task<StoredValidationPlan> IssueAsync(VerifiedSession session,int file,Guid request,ValidationPlanPayload payload,
        string text,UploadValidationPlanSigner signer,bool noOp,CancellationToken ct)=>Authorized(session,file,payload.Review,async(db,sourceExpiry,token)=>
    {
        var prior=await Read(db,session,request,null,token);
        if(prior is not null)
        {
            if(prior.Envelope.RequestHash!=payload.RequestHash||prior.Envelope.CorrectionKey!=payload.CorrectionKey)
                throw new ValidationPlanRejected("plan_request_reused");
            Validate(prior,signer,Now);await VerifyCurrent(db,payload,token);return prior;
        }
        await VerifyCurrent(db,payload,token);
        // Cleanup is scope-bound and bounded by the enforced receipt count.
        foreach(string sql in new[]{
            "DELETE FROM public.upload_validation_plans WHERE plan_id IN(SELECT plan_id FROM public.upload_validation_plans WHERE client_id=$1 AND template_id=$2 AND expires_at<=now() ORDER BY expires_at LIMIT 128)",
            "DELETE FROM public.platform_upload_validation_plans WHERE request_id IN(SELECT request_id FROM public.platform_upload_validation_plans WHERE client_id=$1 AND template_id=$2 AND created_at<now()-interval '24 hours' ORDER BY created_at LIMIT 128)"})
        {
            await using var cleanup=db.CreateCommand(sql);cleanup.Parameters.AddWithValue(payload.Review.Coverage.Footing.Validation.Source.ClientId);
            cleanup.Parameters.AddWithValue(payload.Review.Coverage.Footing.Validation.Source.TemplateId);await cleanup.ExecuteNonQueryAsync(token);
        }
        var source=payload.Review.Coverage.Footing.Validation.Source;
        await using(var capacity=db.CreateCommand("SELECT count(*) FROM public.platform_upload_validation_plans WHERE client_id=$1 AND template_id=$2"))
        {capacity.Parameters.AddWithValue(source.ClientId);capacity.Parameters.AddWithValue(source.TemplateId);if((long)(await capacity.ExecuteScalarAsync(token))!>=options.Value.MaximumReceiptsPerScope) throw new ValidationPlanRejected("plan_receipt_capacity");}
        await using(var size=db.CreateCommand("SELECT pg_database_size(current_database())"))
            if((long)(await size.ExecuteScalarAsync(token))!+4L*System.Text.Encoding.UTF8.GetByteCount(text)+1048576
                >=Math.Min(options.Value.MaximumDatabaseBytes,configuration?.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??options.Value.MaximumDatabaseBytes))
                throw new ValidationPlanRejected("plan_storage_capacity");
        // Keep compact old receipts for idempotency; discard their transaction payloads.
        await using(var supersede=db.CreateCommand("UPDATE public.upload_validation_plans SET status='superseded',plan_json='{}'::jsonb WHERE client_id=$1 AND template_id=$2 AND status='ready'"))
        {supersede.Parameters.AddWithValue(source.ClientId);supersede.Parameters.AddWithValue(source.TemplateId);await supersede.ExecuteNonQueryAsync(token);}
        var now=Now;var expiry=now.AddMinutes(30);if(sourceExpiry<expiry) expiry=sourceExpiry;
        if(expiry<=now) throw new ValidationPlanRejected("plan_source_expired");
        var envelope=new ValidationPlanEnvelope(UploadValidationPlanSigner.Format,request,Guid.NewGuid().ToString("N"),session.TenantId!.Value,session.UserId,
            source.ClientId,source.TemplateId,file,payload.RequestHash,UploadIdentityValue.Hash(noOp?"":text),payload.CorrectionKey,
            source.ConfigurationKey!,source.MappingRevision!.Value,source.Sha256!,now,expiry,noOp,payload.Counts);
        string signature=signer.Sign(envelope);
        if(!noOp)
        {
            await using var plan=db.CreateCommand("""
                INSERT INTO public.upload_validation_plans(plan_id,file_id,client_id,template_id,file_sha256,request_signature,plan_json,status,created_at,expires_at)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,'ready',$8,$9)
                """);
            plan.Parameters.AddWithValue(envelope.PlanId);plan.Parameters.AddWithValue(file);plan.Parameters.AddWithValue(source.ClientId);plan.Parameters.AddWithValue(source.TemplateId);
            plan.Parameters.AddWithValue(source.Sha256!);plan.Parameters.AddWithValue(payload.RequestHash);
            // A lossless payload string survives JSONB's numeric normalization.
            plan.Parameters.AddWithValue(JsonSerializer.Serialize(new {format=UploadValidationPlanSigner.Format,payload=text}));
            plan.Parameters.AddWithValue(now);plan.Parameters.AddWithValue(expiry);await plan.ExecuteNonQueryAsync(token);
        }
        await using(var receipt=db.CreateCommand("""
            INSERT INTO public.platform_upload_validation_plans(request_id,plan_id,tenant_id,user_id,client_id,template_id,file_id,envelope,signature,created_at,expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """))
        {
            receipt.Parameters.AddWithValue(request);receipt.Parameters.AddWithValue(envelope.PlanId);receipt.Parameters.AddWithValue(envelope.Tenant);receipt.Parameters.AddWithValue(envelope.User);
            receipt.Parameters.AddWithValue(envelope.Client);receipt.Parameters.AddWithValue(envelope.Template);receipt.Parameters.AddWithValue(file);
            receipt.Parameters.AddWithValue(JsonSerializer.Serialize(envelope));receipt.Parameters.AddWithValue(signature);receipt.Parameters.AddWithValue(now);receipt.Parameters.AddWithValue(expiry);
            await receipt.ExecuteNonQueryAsync(token);
        }
        return new(envelope,signature,noOp?"":text,noOp?"no_changes":"ready");
    },ct);

    internal Task<StoredValidationPlan> ReconcileAsync(VerifiedSession session,int file,Guid request,CorrectionRequest review,CancellationToken ct)=>
        Authorized(session,file,review,async(db,_,token)=>await Read(db,session,request,null,token)??throw new ValidationPlanRejected("plan_outcome_unknown"),ct);

    internal Task VerifyAsync(VerifiedSession session,int file,StoredValidationPlan expected,ValidationPlanPayload payload,UploadValidationPlanSigner signer,CancellationToken ct)=>
        Authorized(session,file,payload.Review,async(db,_,token)=>
        {
            var current=await Read(db,session,null,expected.Envelope.PlanId,token)??throw new ValidationPlanRejected("plan_not_found");
            Validate(current,signer,Now);
            if(current.Envelope!=expected.Envelope||current.Payload!=expected.Payload||current.Signature!=expected.Signature) throw new ValidationPlanRejected("plan_changed");
            await VerifyCurrent(db,payload,token);return true;
        },ct);

    internal static void Validate(StoredValidationPlan plan,UploadValidationPlanSigner signer,DateTimeOffset now)
    {
        if(!signer.Verify(plan)) throw new ValidationPlanRejected("plan_signature_invalid");
        if(plan.Envelope.ExpiresAt<=now) throw new ValidationPlanRejected("plan_expired");
        if(plan.State is not ("ready" or "no_changes")) throw new ValidationPlanRejected("plan_not_ready");
    }

    // Same scope/scalars as P22; compare every captured current-row guard under
    // the shared writer lock. P25 must repeat this inside its atomic import.
    internal async Task VerifyCurrent(PostgresTransactionContext db,ValidationPlanPayload payload,CancellationToken ct)
    {
        var source=payload.Review.Coverage.Footing.Validation.Source;bool complete=payload.Review.Coverage.Footing.Validation.ComparisonMode=="replace_range";
        await using(var command=db.CreateCommand("""
            SELECT id,CASE WHEN octet_length(row_data::text)<=$6 THEN row_data::text END,COALESCE(version,1),document_group_id,
                source_identity,source_row_number,source_order_upload_id,extract_date::text,change_detection_status
            FROM public.client_data WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE
              AND ($3::date IS NULL OR extract_date BETWEEN $3::date AND $4::date OR document_group_id=ANY($5)) ORDER BY id LIMIT $7 FOR UPDATE
            """))
        {
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            command.Parameters.AddWithValue(NpgsqlDbType.Date,complete?DateOnly.Parse(payload.Review.Coverage.CoverageStart!,CultureInfo.InvariantCulture):DBNull.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Date,complete?DateOnly.Parse(payload.Review.Coverage.CoverageEnd!,CultureInfo.InvariantCulture):DBNull.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,payload.IncomingGroups.ToArray());command.Parameters.AddWithValue(options.Value.MaximumPayloadBytes);
            command.Parameters.AddWithValue(options.Value.MaximumRows+1);
            var guards=payload.Guards.OrderBy(g=>g.Id).ToArray();int count=0;long bytes=0;
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                if(count>=guards.Length||reader.IsDBNull(1)) throw new ValidationPlanRejected("plan_data_changed");
                string raw=reader.GetString(1);bytes+=System.Text.Encoding.UTF8.GetByteCount(raw);
                if(bytes>67108864) throw new ValidationPlanRejected("plan_comparison_capacity");
                string? Text(int i)=>reader.IsDBNull(i)?null:reader.GetString(i);
                var row=new IdentityStoredRow(reader.GetInt32(0),UploadIdentityValue.FromJson(raw),Text(3),Text(4),reader.GetInt32(2),
                    reader.IsDBNull(5)?null:reader.GetInt32(5),reader.IsDBNull(6)?null:reader.GetInt32(6),Text(7));
                if(Text(8)=="deleted"||row.Id!=guards[count].Id||UploadValidationPlanSigner.GuardHash(row)!=guards[count++].Hash) throw new ValidationPlanRejected("plan_data_changed");
            }
            if(count!=guards.Length) throw new ValidationPlanRejected("plan_data_changed");
        }
        if(complete&&payload.ChangedGroups.Count>0)
        {
            await using var command=db.CreateCommand("""
                SELECT id FROM public.client_data WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE
                AND document_group_id=ANY($3) ORDER BY id LIMIT $4
                """);
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            command.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,payload.ChangedGroups.ToArray());command.Parameters.AddWithValue(options.Value.MaximumRows+1);
            var ids=payload.Guards.Select(g=>g.Id).ToHashSet();int count=0;await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct)) if(++count>options.Value.MaximumRows||!ids.Contains(reader.GetInt32(0))) throw new ValidationPlanRejected("plan_voucher_changed");
        }
    }
}
