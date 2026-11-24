using System.Globalization;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Data;
using NpgsqlTypes;
namespace AuditPlatform.Api.Uploads;

// No commit, new connection or transaction copies. All derivations, compact
// voucher decisions and invalidation share the import's borrowed transaction.
public static class UploadDerivedFieldWriter
{
    // Configuration writers acquire these locks before changing Treasury mappings.
    // Matching import lock order keeps reclassification and source imports serialized.
    public static async Task LockForTreasuryConfigurationChangeAsync(PostgresTransactionContext db,CancellationToken ct)
    {
        await using(var source=db.CreateCommand("LOCK TABLE public.client_data IN SHARE ROW EXCLUSIVE MODE")) await source.ExecuteNonQueryAsync(ct);
        await using(var configuration=db.CreateCommand("LOCK TABLE public.account_control_cashflow_treasury_accounts,public.account_control_cashflow_settings,public.account_control_cashflow_source_mappings,public.platform_tenant_settings IN SHARE ROW EXCLUSIVE MODE"))
            await configuration.ExecuteNonQueryAsync(ct);
    }

    // Call after an effective mapping change, in the same transaction and after
    // LockForTreasuryConfigurationChangeAsync. No transaction copies are rebuilt.
    public static async Task<int> RefreshAfterTreasuryConfigurationChangeAsync(PostgresTransactionContext db,int tenant,int client,int template,
        UploadDerivedProfile previousProfile,UploadDerivedProfile profile,string actor,bool mappingChanged,CancellationToken ct)
    {
        if(!mappingChanged) return 0;
        if(client<1||template<1||tenant<1) throw new ArgumentOutOfRangeException(nameof(client));
        if(!profile.Keys.ContainsKey("account_code")) throw new IdentityReview("derived_journal_bindings_required");
        var current=await UploadDerivedFieldService.LoadAsync(db,tenant,client,template,profile,ct);
        if(!previousProfile.CashflowSource||!current.CashflowSource||previousProfile.ContextKey is null||current.ContextKey is null)
            throw new IdentityReview("derived_cashflow_binding_review_required");
        if(!SameNonTreasuryProfile(previousProfile,current)) throw new ValidationPlanRejected("derived_configuration_changed");
        if(previousProfile.ContextKey==current.ContextKey) return 0;

        var rows=new List<(int Id,string? Treasury,string? TreasuryAccount,string Status)>();long bytes=0;
        await using(var command=db.CreateCommand("""
            SELECT id,row_data::text,derived_configuration_hash FROM public.client_data
            WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE AND change_detection_status<>'deleted'
            ORDER BY id LIMIT 100001 FOR UPDATE
            """))
        {
            command.Parameters.AddWithValue(client);command.Parameters.AddWithValue(template);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string raw=reader.GetString(1);bytes+=Encoding.UTF8.GetByteCount(raw);
                if(rows.Count>=100000||bytes>67108864) throw new IdentityReview("derived_comparison_capacity");
                string? oldHash=reader.IsDBNull(2)?null:reader.GetString(2);
                if(oldHash is not null&&oldHash!=previousProfile.ContextKey||oldHash is null&&previousProfile.RateMode=="derived")
                    throw new IdentityReview("derived_retained_rows_review_required");
                var classification=UploadDerivedFieldService.ClassifyTreasury(current,UploadDerivedFieldService.AccountCode(current,UploadIdentityValue.FromJson(raw)));
                rows.Add((reader.GetInt32(0),classification.Type,classification.TreasuryAccount,classification.Status));
            }
        }
        int written=0;
        foreach(var batch in rows.Chunk(250))
        {
            var values=batch.Select(r=>new {id=r.Id,treasury=r.Treasury,treasury_account=r.TreasuryAccount,status=r.Status});
            await using var command=db.CreateCommand("""
                UPDATE public.client_data c SET derived_treasury_type=v.treasury,derived_treasury_account_code=v.treasury_account,
                    derived_classification_status=v.status,derived_version=1,derived_configuration_hash=$3
                FROM jsonb_to_recordset($1) AS v(id int,treasury text,treasury_account text,status text)
                WHERE c.id=v.id AND c.client_id=$2 AND c.template_id=$4
                """);
            command.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(values));command.Parameters.AddWithValue(client);
            command.Parameters.AddWithValue(current.ContextKey);command.Parameters.AddWithValue(template);
            written+=await command.ExecuteNonQueryAsync(ct);
        }
        if(written!=rows.Count) throw new ValidationPlanRejected("derived_counts_changed");
        await using(var invalidate=db.CreateCommand("DELETE FROM public.cash_bank_summary_read_models WHERE client_id=$1"))
        {invalidate.Parameters.AddWithValue(client);await invalidate.ExecuteNonQueryAsync(ct);}
        await using(var revision=db.CreateCommand("""
            INSERT INTO public.cashflow_report_revisions(client_id,revision,changed_at,changed_by,change_reason)
            VALUES($1,2,now(),$2,'native_treasury_configuration_changed') ON CONFLICT(client_id) DO UPDATE SET
                revision=public.cashflow_report_revisions.revision+1,changed_at=now(),changed_by=EXCLUDED.changed_by,change_reason=EXCLUDED.change_reason
            """))
        {revision.Parameters.AddWithValue(client);revision.Parameters.AddWithValue(actor);await revision.ExecuteNonQueryAsync(ct);}
        return rows.Count;
    }

    private static bool SameNonTreasuryProfile(UploadDerivedProfile before,UploadDerivedProfile after)=>
        before.TemplateKey==after.TemplateKey&&before.RateMode==after.RateMode&&before.Precision==after.Precision
        &&before.CashflowSource==after.CashflowSource
        &&before.Keys.OrderBy(p=>p.Key,StringComparer.Ordinal).SequenceEqual(after.Keys.OrderBy(p=>p.Key,StringComparer.Ordinal))
        &&before.ColumnIds.OrderBy(p=>p.Key,StringComparer.Ordinal).SequenceEqual(after.ColumnIds.OrderBy(p=>p.Key,StringComparer.Ordinal))
        &&(before.NumericKeys??[]).SequenceEqual(after.NumericKeys??[]);

    public static async Task GuardAsync(PostgresTransactionContext db,ValidationPlanPayload payload,int tenant,CancellationToken ct)
    {
        if(payload.ImportProfile?.Derivation is not {} profile) return;
        if(profile.RateMode=="derived")
        {await using var command=db.CreateCommand("LOCK TABLE public.platform_tenant_settings IN SHARE MODE");await command.ExecuteNonQueryAsync(ct);}
        if(profile.Keys.ContainsKey("account_code"))
        {
            await using var command=db.CreateCommand("LOCK TABLE public.account_control_cashflow_treasury_accounts,public.account_control_cashflow_settings,public.account_control_cashflow_source_mappings IN SHARE MODE");
            await command.ExecuteNonQueryAsync(ct);
        }
        var source=payload.Review.Coverage.Footing.Validation.Source;
        var current=await UploadDerivedFieldService.LoadAsync(db,tenant,source.ClientId,source.TemplateId,profile,ct);
        if(current.ContextKey!=profile.ContextKey) throw new ValidationPlanRejected("derived_configuration_changed");
        if(profile.Keys.ContainsKey("account_code"))
        {
            await using var retained=db.CreateCommand("""
                SELECT EXISTS(SELECT 1 FROM public.client_data WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE
                    AND change_detection_status<>'deleted' AND NOT(document_group_id=ANY($3)) AND derived_configuration_hash IS DISTINCT FROM $4)
                """);
            retained.Parameters.AddWithValue(source.ClientId);retained.Parameters.AddWithValue(source.TemplateId);
            retained.Parameters.AddWithValue(payload.IncomingGroups.Concat(payload.ChangedGroups).Distinct(StringComparer.Ordinal).ToArray());retained.Parameters.AddWithValue(profile.ContextKey);
            if(await retained.ExecuteScalarAsync(ct) is true) throw new IdentityReview("derived_retained_rows_review_required");
        }
    }

    public static async Task ApplyAsync(PostgresTransactionContext db,ValidationPlanPayload payload,int file,string actor,CancellationToken ct)
    {
        if(payload.ImportProfile?.Derivation is not {} profile) return;
        var source=payload.Review.Coverage.Footing.Validation.Source;
        string[] groups=payload.IncomingGroups.Concat(payload.ChangedGroups).Distinct(StringComparer.Ordinal).ToArray();
        var rows=new List<(int Id,string Group,UploadDerivedValues Value)>();long bytes=0;
        await using(var command=db.CreateCommand("""
            SELECT id,row_data::text,extract_date::text,document_group_id FROM public.client_data
            WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE AND change_detection_status<>'deleted'
                AND document_group_id=ANY($3) ORDER BY id LIMIT 100001 FOR UPDATE
            """))
        {
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);command.Parameters.AddWithValue(groups);
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string raw=reader.GetString(1);bytes+=Encoding.UTF8.GetByteCount(raw);
                if(rows.Count>=100000||bytes>67108864) throw new IdentityReview("derived_comparison_capacity");
                rows.Add((reader.GetInt32(0),reader.GetString(3),UploadDerivedFieldService.Project(profile,UploadIdentityValue.FromJson(raw),reader.IsDBNull(2)?null:reader.GetString(2))));
            }
        }
        int written=0;
        foreach(var batch in rows.Chunk(250))
        {
            var values=batch.Select(r=>new {id=r.Id,voucher=r.Value.Voucher,account=r.Value.Account,name=r.Value.AccountName,date=r.Value.Date,month=r.Value.Month,
                fortnight=r.Value.Fortnight,debit=r.Value.Debit,credit=r.Value.Credit,amount=r.Value.Amount,treasury=r.Value.TreasuryType,treasury_account=r.Value.TreasuryAccount,status=r.Value.Status});
            await using var command=db.CreateCommand("""
                UPDATE public.client_data c SET extract_amount=v.amount::real,derived_voucher_id=v.voucher,derived_account_code=v.account,derived_account_name=v.name,
                    derived_transaction_date=v.date::date,derived_transaction_month=v.month,derived_transaction_fortnight=v.fortnight,
                    derived_debit=v.debit,derived_credit=v.credit,derived_treasury_type=v.treasury,derived_treasury_account_code=v.treasury_account,
                    derived_classification_status=v.status,derived_version=1,derived_configuration_hash=$4,
                    rate_source=CASE WHEN c.upload_id=$5 THEN $6 ELSE c.rate_source END
                FROM jsonb_to_recordset($1) AS v(id int,voucher text,account text,name text,date text,month text,fortnight text,
                    debit double precision,credit double precision,amount double precision,treasury text,treasury_account text,status text)
                WHERE c.id=v.id AND c.client_id=$2 AND c.template_id=$3
                """);
            command.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(values));command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            command.Parameters.AddWithValue(profile.ContextKey);command.Parameters.AddWithValue(file);command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)profile.RateMode??DBNull.Value);
            written+=await command.ExecuteNonQueryAsync(ct);
        }
        if(written!=rows.Count) throw new ValidationPlanRejected("derived_counts_changed");
        if(profile.CashflowSource)
        {
            await using(var table=db.CreateCommand("LOCK TABLE public.cashflow_voucher_occurrences IN SHARE ROW EXCLUSIVE MODE")) await table.ExecuteNonQueryAsync(ct);
            await PersistOccurrences(db,payload,file,rows,ct);
        }
        // The compatibility revision must actually advance. Older calculation helpers
        // only update timestamps; never treat their constant revision as proof.
        if(profile.Keys.ContainsKey("account_code"))
        {
            await using(var invalidate=db.CreateCommand("DELETE FROM public.cash_bank_summary_read_models WHERE client_id=$1"))
            {invalidate.Parameters.AddWithValue(source.ClientId);await invalidate.ExecuteNonQueryAsync(ct);}
            await using var revision=db.CreateCommand("""
                INSERT INTO public.cashflow_report_revisions(client_id,revision,changed_at,changed_by,change_reason)
                VALUES($1,2,now(),$2,'native_source_imported') ON CONFLICT(client_id) DO UPDATE SET
                    revision=public.cashflow_report_revisions.revision+1,changed_at=now(),changed_by=EXCLUDED.changed_by,change_reason=EXCLUDED.change_reason
                """);
            revision.Parameters.AddWithValue(source.ClientId);revision.Parameters.AddWithValue(actor);await revision.ExecuteNonQueryAsync(ct);
        }
    }

    private static async Task PersistOccurrences(PostgresTransactionContext db,ValidationPlanPayload payload,int file,
        IReadOnlyList<(int Id,string Group,UploadDerivedValues Value)> rows,CancellationToken ct)
    {
        var source=payload.Review.Coverage.Footing.Validation.Source;
        string sequence;
        await using(var command=db.CreateCommand("SELECT to_char(upload_date AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US') FROM public.uploaded_files WHERE id=$1"))
        {command.Parameters.AddWithValue(file);sequence=(string?)await command.ExecuteScalarAsync(ct)??throw new IdentityReview("derived_upload_time_required");}
        var incoming=new List<UploadVoucherDecision>();
        foreach(var group in rows.Where(r=>payload.IncomingGroups.Contains(r.Group,StringComparer.Ordinal)).GroupBy(r=>r.Group,StringComparer.Ordinal))
        {
            if(payload.IncomingGroupCounts is null||payload.IncomingGroupCounts.GetValueOrDefault(group.Key)!=group.Count()) throw new IdentityReview("derived_partial_voucher_review_required");
            incoming.Add(UploadDerivedFieldService.Occurrence(group.Select(r=>r.Value).ToArray(),file,sequence));
        }
        if(incoming.Select(r=>r.Identity).Distinct(StringComparer.Ordinal).Count()!=incoming.Count) throw new IdentityReview("derived_voucher_identity_review_required");
        var existing=new List<UploadVoucherDecision>();
        await using(var command=db.CreateCommand("""
            SELECT voucher_id,voucher_identity,occurrence_key,voucher_fingerprint,transaction_date::text,source_line_count,upload_id,
                to_char(source_extract_as_of AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US'),decision_status,decision_reason
            FROM public.cashflow_voucher_occurrences WHERE client_id=$1 AND template_id=$2 AND voucher_identity=ANY($3) ORDER BY id LIMIT 100001 FOR UPDATE
            """))
        {
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);command.Parameters.AddWithValue(incoming.Select(r=>r.Identity).ToArray());
            await using var reader=await command.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                if(existing.Count>=100000) throw new IdentityReview("derived_occurrence_capacity");
                if(reader.IsDBNull(0)||reader.IsDBNull(1)||reader.IsDBNull(4)||reader.IsDBNull(7)||reader.IsDBNull(9))
                    throw new IdentityReview("derived_occurrence_ancestry_review_required");
                existing.Add(new(reader.GetString(0),reader.GetString(1),reader.GetString(2),reader.GetString(3),reader.IsDBNull(4)?null:reader.GetString(4),reader.GetInt64(5),reader.GetInt32(6),reader.GetString(7),reader.GetString(8),reader.GetString(9)));
            }
        }
        var incomingByKey=incoming.ToDictionary(i=>i.Key,StringComparer.Ordinal);
        if(existing.Any(e=>incomingByKey.TryGetValue(e.Key,out var i)&&(i.Fingerprint!=e.Fingerprint||i.Lines!=e.Lines)))
            throw new IdentityReview("derived_occurrence_changed");
        var decisions=UploadDerivedFieldService.Resolve(existing.Where(e=>!incomingByKey.ContainsKey(e.Key)).Concat(incoming));
        foreach(var batch in decisions.Chunk(250))
        {
            await using var command=db.CreateCommand("""
                INSERT INTO public.cashflow_voucher_occurrences(client_id,template_id,upload_id,voucher_id,voucher_identity,occurrence_key,voucher_fingerprint,
                    fingerprint_version,transaction_date,source_line_count,source_extract_as_of,decision_status,decision_reason,resolved_at)
                SELECT $2,$3,v."Upload",v."Voucher",v."Identity",v."Key",v."Fingerprint",1,v."Date"::date,v."Lines",v."Sequence"::timestamptz,v."Status",v."Reason",now()
                FROM jsonb_to_recordset($1) AS v("Upload" int,"Voucher" text,"Identity" text,"Key" text,"Fingerprint" text,"Date" text,"Lines" bigint,"Sequence" text,"Status" text,"Reason" text)
                ON CONFLICT(client_id,template_id,upload_id,occurrence_key) DO UPDATE SET decision_status=EXCLUDED.decision_status,
                    decision_reason=EXCLUDED.decision_reason,canonical_occurrence_id=NULL,resolved_at=now()
                """);
            command.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(batch));command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            if(await command.ExecuteNonQueryAsync(ct)!=batch.Length) throw new ValidationPlanRejected("derived_occurrence_counts_changed");
        }
        foreach(var batch in decisions.Chunk(250))
        {
            await using var command=db.CreateCommand("""
                UPDATE public.cashflow_voucher_occurrences t SET canonical_occurrence_id=c.id FROM
                jsonb_to_recordset($1) AS v("Key" text,"Canonical" text),public.cashflow_voucher_occurrences c
                WHERE t.client_id=$2 AND t.template_id=$3 AND t.occurrence_key=v."Key"
                    AND c.client_id=$2 AND c.template_id=$3 AND c.occurrence_key=v."Canonical"
                """);
            command.Parameters.AddWithValue(NpgsqlDbType.Jsonb,JsonSerializer.Serialize(batch));command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            if(await command.ExecuteNonQueryAsync(ct)!=batch.Length) throw new ValidationPlanRejected("derived_occurrence_counts_changed");
        }
        // Complete replacement can retire a voucher entirely. Retain its compact
        // audit decision, but never leave it selectable as a current voucher.
        if(payload.Review.Coverage.Footing.Validation.ComparisonMode=="replace_range")
        {
            await using var command=db.CreateCommand("""
                UPDATE public.cashflow_voucher_occurrences o SET decision_status='superseded',decision_reason='removed_by_complete_coverage',canonical_occurrence_id=NULL,resolved_at=now()
                WHERE o.client_id=$1 AND o.template_id=$2 AND o.transaction_date BETWEEN $3::date AND $4::date
                    AND o.decision_status IN('current','current_revised') AND NOT EXISTS(SELECT 1 FROM public.client_data c
                        WHERE c.client_id=$1 AND c.template_id=$2 AND c.is_deleted IS FALSE AND c.change_detection_status<>'deleted'
                            AND upper(regexp_replace(c.derived_voucher_id,'\s+','','g'))=o.voucher_identity)
                """);
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);command.Parameters.AddWithValue(payload.Review.Coverage.CoverageStart!);command.Parameters.AddWithValue(payload.Review.Coverage.CoverageEnd!);
            await command.ExecuteNonQueryAsync(ct);
        }
    }
}
