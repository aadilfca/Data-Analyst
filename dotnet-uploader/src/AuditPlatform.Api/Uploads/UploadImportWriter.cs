using System.Globalization;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using NpgsqlTypes;
namespace AuditPlatform.Api.Uploads;

// Only this writer mutates canonical rows. Every command borrows the caller's
// transaction; bounded batches avoid one network trip per spreadsheet row.
public sealed class UploadImportWriter(IOptions<UploadImportOptions> options,IConfiguration? configuration=null)
{
    private sealed record Mutation(int id,string action,string? raw,string? fingerprint,string? date,string group,
        string identity,int? position,int version,string fields);

    internal async Task ApplyAsync(PostgresTransactionContext db,ValidationPlanPayload payload,int file,Guid operation,string actor,CancellationToken ct)
    {
        var source=payload.Review.Coverage.Footing.Validation.Source;
        if(payload.ImportProfile is not {} profile||profile.AccountCode is not null) throw new ValidationPlanRejected("import_bindings_review_required");
        await using(var purpose=db.CreateCommand("SELECT data_purpose,rate_source FROM public.templates WHERE id=$1 FOR SHARE"))
        {
            purpose.Parameters.AddWithValue(source.TemplateId);await using var reader=await purpose.ExecuteReaderAsync(ct);
            if(!await reader.ReadAsync(ct)||reader.GetString(0)!="standard"||!reader.IsDBNull(1)&&profile.Derivation is null)
                throw new ValidationPlanRejected("import_derived_fields_required");
        }
        // Table locks also cover writers which do not yet use our advisory lock.
        // They prevent phantom evidence/identity changes between guards and writes.
        await using(var locks=db.CreateCommand("LOCK TABLE public.client_data, public.verification_details, public.voucher_line_documents, public.voucher_line_physical_bill_items, public.cashflow_prepared_lines IN SHARE ROW EXCLUSIVE MODE"))
            await locks.ExecuteNonQueryAsync(ct);
        await RequireIdentityIndex(db,ct);
        var existing=new Dictionary<int,IReadOnlyDictionary<string,IdentityAtom>>();
        var mutations=new List<Mutation>();
        int sourceOrderUpdates=0;
        long metadataBytes=0;
        int[] ids=payload.Actions.Where(a=>a.Action.ExistingId is not null).Select(a=>checked((int)a.Action.ExistingId!.Value)).ToArray();
        if(ids.Distinct().Count()!=ids.Length) throw new ValidationPlanRejected("import_actions_invalid");
        var actionsById=payload.Actions.Where(a=>a.Action.ExistingId is not null).ToDictionary(a=>checked((int)a.Action.ExistingId!.Value));
        if(ids.Length>0)
        {
            await using var rows=db.CreateCommand("SELECT id,row_data::text,to_jsonb(c)-'row_data' FROM public.client_data c WHERE client_id=$1 AND template_id=$2 AND id=ANY($3) ORDER BY id FOR UPDATE");
            rows.Parameters.AddWithValue(source.ClientId);rows.Parameters.AddWithValue(source.TemplateId);rows.Parameters.AddWithValue(ids);
            await using var reader=await rows.ExecuteReaderAsync(ct);
            while(await reader.ReadAsync(ct))
            {
                string metadataText=reader.GetString(2);metadataBytes+=Encoding.UTF8.GetByteCount(metadataText);
                using var metadata=JsonDocument.Parse(metadataText);
                var action=actionsById[reader.GetInt32(0)].Action;
                var previousPosition=metadata.RootElement.GetProperty("source_row_number");
                if(action.Action=="unchanged"&&(previousPosition.ValueKind==JsonValueKind.Null||previousPosition.GetInt32()!=action.Row
                    ||metadata.RootElement.GetProperty("source_order_upload_id").ValueKind==JsonValueKind.Null)) sourceOrderUpdates++;
                // P26 owns derived report/classification maintenance. Stop rather
                // than retain stale derived values on an already-populated row.
                if(profile.Derivation is null&&metadata.RootElement.EnumerateObject().Any(p=>(p.Name.StartsWith("derived_",StringComparison.Ordinal)||p.Name=="extract_amount")&&p.Value.ValueKind!=JsonValueKind.Null))
                    throw new ValidationPlanRejected("import_derived_fields_required");
                existing.Add(reader.GetInt32(0),UploadIdentityValue.FromJson(reader.GetString(1)));
            }
        }
        if(existing.Count!=ids.Length) throw new ValidationPlanRejected("plan_data_changed");
        long projected=metadataBytes+payload.Actions.Count*1024L+payload.Actions.Sum(a=>a.Values is null?0L:System.Text.Encoding.UTF8.GetByteCount(RawJson(a.Values)))
            +existing.Values.Sum(v=>(long)System.Text.Encoding.UTF8.GetByteCount(RawJson(v)));
        if(profile.Derivation is not null)
        {
            // Metadata-only plans can refresh many unchanged rows. Reserve their
            // rewrite/index cost too, rather than budgeting only financial actions.
            await using var estimate=db.CreateCommand("SELECT COALESCE(sum(pg_column_size(c)),0)::bigint+count(*)*2048 FROM public.client_data c WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE AND document_group_id=ANY($3)");
            estimate.Parameters.AddWithValue(source.ClientId);estimate.Parameters.AddWithValue(source.TemplateId);
            estimate.Parameters.AddWithValue(payload.IncomingGroups.Concat(payload.ChangedGroups).Distinct(StringComparer.Ordinal).ToArray());
            projected+=(long)(await estimate.ExecuteScalarAsync(ct))!;
            if(profile.Derivation.CashflowSource)
            {
                await using var history=db.CreateCommand("SELECT count(*)*2048 FROM public.cashflow_voucher_occurrences WHERE client_id=$1 AND template_id=$2");
                history.Parameters.AddWithValue(source.ClientId);history.Parameters.AddWithValue(source.TemplateId);projected+=(long)(await history.ExecuteScalarAsync(ct))!;
            }
        }
        await using(var capacity=db.CreateCommand("SELECT pg_database_size(current_database())"))
            if((long)(await capacity.ExecuteScalarAsync(ct))!+projected*4+1048576>=Math.Min(500000000,configuration?.GetValue<long?>("Platform:DatabaseCapacityLimitBytes")??500000000))
                throw new ValidationPlanRejected("import_storage_capacity");
        int[] removed=payload.Actions.Where(a=>a.Action.Action=="removed").Select(a=>checked((int)a.Action.ExistingId!.Value)).ToArray();
        if(removed.Length>0&&payload.Review.Coverage.Footing.Validation.ComparisonMode!="replace_range") throw new ValidationPlanRejected("import_partial_removal");
        foreach(var (table,column) in new[]{("verification_details","client_data_id"),("voucher_line_documents","client_data_id"),
            ("voucher_line_physical_bill_items","source_document_row_id"),("cashflow_prepared_lines","source_client_data_id")})
        {
            await using var guard=db.CreateCommand($"SELECT EXISTS(SELECT 1 FROM public.{table} WHERE {column}=ANY($1))");
            guard.Parameters.AddWithValue(removed);
            if((bool)(await guard.ExecuteScalarAsync(ct))!) throw new ValidationPlanRejected("import_protected_evidence");
        }
        // Existing downstream copies/decisions need P26 invalidation. Optional
        // retired stores are inspected only when present and are never recreated.
        foreach(string table in new[]{"cash_bank_transaction_classifications","cashflow_voucher_occurrences","cashflow_prepared_rollups","cash_bank_summary_read_models","cashflow_ledger_rollup_read_models"})
        {
            if(profile.Derivation is not null&&(table=="cash_bank_summary_read_models"||table=="cashflow_voucher_occurrences"&&profile.Derivation.CashflowSource)) continue;
            await using var exists=db.CreateCommand("SELECT to_regclass($1) IS NOT NULL");exists.Parameters.AddWithValue("public."+table);
            if(!(bool)(await exists.ExecuteScalarAsync(ct))!) continue;
            await using(var tableLock=db.CreateCommand($"LOCK TABLE public.{table} IN SHARE ROW EXCLUSIVE MODE")) await tableLock.ExecuteNonQueryAsync(ct);
            await using var guard=db.CreateCommand($"SELECT EXISTS(SELECT 1 FROM public.{table} WHERE client_id=$1 AND template_id=$2)");
            guard.Parameters.AddWithValue(source.ClientId);guard.Parameters.AddWithValue(source.TemplateId);
            if((bool)(await guard.ExecuteScalarAsync(ct))!) throw new ValidationPlanRejected("import_downstream_refresh_required");
        }
        foreach(var item in payload.Actions)
        {
            ct.ThrowIfCancellationRequested();var a=item.Action;
            if(a.Action is not ("added" or "changed" or "removed" or "unchanged")||a.Identity.Length==0||a.DocumentGroup.Length==0)
                throw new ValidationPlanRejected("import_actions_invalid");
            int id=a.ExistingId is {} value?checked((int)value):0;
            var old=id==0?null:existing[id];
            if((a.Action=="added")!=(id==0)||a.Action!="added"&&a.ExpectedVersion is not >0) throw new ValidationPlanRejected("import_actions_invalid");
            Dictionary<string,IdentityAtom>? data=null;
            if(a.Action!="removed")
            {
                if(a.Row is not >1) throw new ValidationPlanRejected("import_actions_invalid");
                if(a.Action=="unchanged")
                {
                    if(item.Values is not null&&item.Values.Keys.Any(k=>!profile.Columns.Any(c=>c.Key==k&&!c.Compare)))
                        throw new ValidationPlanRejected("import_information_fields_invalid");
                    data=new(old!,StringComparer.Ordinal);if(item.Values is not null) foreach(var pair in item.Values) data[pair.Key]=pair.Value;
                }
                else data=new(item.Values??throw new ValidationPlanRejected("import_values_missing"),StringComparer.Ordinal);
            }
            var fields=new Dictionary<string,object?>();
            foreach(string key in a.Action=="removed"?old!.Keys:a.ChangedFields)
                fields[key]=new {old_value=Scalar(old?.GetValueOrDefault(key,new("blank"))??new("blank")),new_value=Scalar(data?.GetValueOrDefault(key,new("blank"))??new("blank"))};
            mutations.Add(new(id,a.Action,data is null?null:RawJson(data),data is null?null:RawFingerprint(data),item.ValidatedDate,a.DocumentGroup,a.Identity,a.Row,
                checked((int)(a.ExpectedVersion??0)),JsonSerializer.Serialize(fields)));
        }
        if(mutations.Count(m=>m.action=="added")!=payload.Counts.Added||mutations.Count(m=>m.action=="changed")!=payload.Counts.Changed
            ||mutations.Count(m=>m.action=="removed")!=payload.Counts.Removed
            ||payload.Actions.Count(a=>a.Action.Action=="unchanged"&&a.Values is {Count:>0})!=payload.Counts.InformationalUpdates
            ||sourceOrderUpdates!=payload.Counts.SourceOrderUpdates||payload.Guards.Count!=payload.Counts.Existing
            ||payload.Counts.Incoming!=payload.Counts.Unchanged+payload.Counts.Changed+payload.Counts.Added
            ||payload.Counts.Existing!=payload.Counts.Unchanged+payload.Counts.Changed+payload.Counts.Removed+payload.Counts.Retained)
            throw new ValidationPlanRejected("import_counts_changed");
        int historyCount=0,changedCount=0,removedCount=0,addedCount=0,metadataCount=0;
        foreach(var batch in mutations.Chunk(options.Value.BatchSize))
        {
            string json=JsonSerializer.Serialize(batch);
            async Task<int> Execute(string sql)
            {
                await using var command=db.CreateCommand(sql);command.Parameters.AddWithValue(NpgsqlDbType.Jsonb,json);command.Parameters.AddWithValue(source.ClientId);
                command.Parameters.AddWithValue(source.TemplateId);command.Parameters.AddWithValue(file);command.Parameters.AddWithValue(operation);command.Parameters.AddWithValue(actor);
                return await command.ExecuteNonQueryAsync(ct);
            }
            const string records="jsonb_to_recordset($1) AS a(id int,action text,raw text,fingerprint text,date text,\"group\" text,identity text,position int,version int,fields text)";
            historyCount+=await Execute($"""
                INSERT INTO public.client_data_history(client_id,template_id,client_data_id,action,changed_by,source_upload_id,replacing_upload_id,source_identity,row_data,changed_fields,import_operation_id,source_metadata)
                SELECT c.client_id,c.template_id,c.id,CASE WHEN a.action='removed' THEN 'deleted' ELSE 'changed' END,$6,c.upload_id,$4,c.source_identity,c.row_data,a.fields::jsonb,$5,to_jsonb(c)-'row_data'
                FROM public.client_data c JOIN {records} ON c.id=a.id WHERE c.client_id=$2 AND c.template_id=$3 AND a.action IN('changed','removed') AND c.version=a.version
                """);
            // Mark every existing review of changed financial data Pending, retaining
            // all IDs, evidence and management/audit work for the required re-review.
            await Execute($"""
                UPDATE public.verification_details v SET validation_status='Pending' FROM {records}
                WHERE v.client_data_id=a.id AND a.action='changed' AND EXISTS(SELECT 1 FROM public.client_data c WHERE c.id=a.id AND c.client_id=$2 AND c.template_id=$3)
                """);
            changedCount+=await Execute($"""
                UPDATE public.client_data c SET row_data=a.raw::jsonb,upload_id=$4,extract_date=a.date::date,document_group_id=a."group",
                    source_identity=a.identity,source_fingerprint=a.fingerprint,source_row_number=a.position,source_order_upload_id=$4,
                    version=c.version+1,change_detection_status='changed'
                FROM {records} WHERE c.id=a.id AND c.client_id=$2 AND c.template_id=$3 AND c.version=a.version AND a.action='changed'
                """);
            metadataCount+=await Execute($"""
                UPDATE public.client_data c SET row_data=a.raw::jsonb,source_fingerprint=a.fingerprint,source_row_number=a.position,source_order_upload_id=$4
                FROM {records} WHERE c.id=a.id AND c.client_id=$2 AND c.template_id=$3 AND c.version=a.version AND a.action='unchanged'
                """);
            await Execute($"""
                DELETE FROM public.data_audit_log d USING {records} WHERE d.client_data_id=a.id AND a.action='removed'
                    AND EXISTS(SELECT 1 FROM public.client_data c WHERE c.id=a.id AND c.client_id=$2 AND c.template_id=$3)
                """);
            removedCount+=await Execute($"""
                DELETE FROM public.client_data c USING {records} WHERE c.id=a.id AND c.client_id=$2 AND c.template_id=$3 AND c.version=a.version AND a.action='removed'
                """);
            addedCount+=await Execute($"""
                INSERT INTO public.client_data(client_id,template_id,upload_id,row_data,extract_date,created_by,is_deleted,version,document_group_id,change_detection_status,source_identity,source_fingerprint,source_row_number,source_order_upload_id)
                SELECT $2,$3,$4,a.raw::jsonb,a.date::date,$6,FALSE,1,a."group",'new',a.identity,a.fingerprint,a.position,$4 FROM {records} WHERE a.action='added'
                """);
        }
        if(historyCount!=payload.Counts.Changed+payload.Counts.Removed||changedCount!=payload.Counts.Changed||removedCount!=payload.Counts.Removed
            ||addedCount!=payload.Counts.Added||metadataCount!=mutations.Count(m=>m.action=="unchanged")) throw new ValidationPlanRejected("import_counts_changed");
    }

    private static async Task RequireIdentityIndex(PostgresTransactionContext db,CancellationToken ct)
    {
        await using var command=db.CreateCommand("""
            SELECT i.indisunique AND i.indisvalid AND i.indisready AND i.indnkeyatts=3
                AND ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(attnum,n)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.n)=ARRAY['client_id','template_id','source_identity']
                AND lower(regexp_replace(pg_get_expr(i.indpred,i.indrelid),'[()[:space:]]','','g')) IN (
                    'is_deleted=falseandchange_detection_status<>''deleted''::textandsource_identityisnotnullandsource_identity<>''''::text',
                    'is_deletedisfalseandchange_detection_status<>''deleted''::textandsource_identityisnotnullandsource_identity<>''''::text')
            FROM pg_index i WHERE i.indexrelid=to_regclass('public.idx_client_data_active_identity_unique') AND i.indrelid='public.client_data'::regclass
            """);
        if(await command.ExecuteScalarAsync(ct) is not true) throw new ValidationPlanRejected("import_identity_index_required");
    }
    private static JsonElement Scalar(IdentityAtom atom)=>JsonSerializer.SerializeToElement(atom.Kind switch {
        "blank"=>null,"text"=>(object?)atom.Text,"boolean"=>atom.Text=="True","number"=>JsonDocument.Parse(atom.Text!).RootElement.Clone(),
        _=>throw new ValidationPlanRejected("import_scalar_invalid")});
    internal static string RawJson(IReadOnlyDictionary<string,IdentityAtom> values)=>JsonSerializer.Serialize(values.ToDictionary(p=>p.Key,p=>Scalar(p.Value),StringComparer.Ordinal));
    public static string RawFingerprint(IReadOnlyDictionary<string,IdentityAtom> values)
    {
        // Legacy raw guard: omit false-like values, Python Unicode sorting and
        // ensure_ascii strings. This never becomes the financial value store.
        string Quote(string text)
        {
            var b=new StringBuilder("\"");foreach(char c in text) b.Append(c switch {'"'=>"\\\"",'\\'=>"\\\\",'\b'=>"\\b",'\f'=>"\\f",'\n'=>"\\n",'\r'=>"\\r",'\t'=>"\\t",
                <' ' or >= '\x7f'=>"\\u"+((int)c).ToString("x4",CultureInfo.InvariantCulture),_=>c.ToString()});return b.Append('"').ToString();
        }
        string Text(IdentityAtom atom)
        {
            if(atom.Kind!="number") return UploadIdentityValue.RawComparison(atom);
            string raw=atom.Text!;if(!raw.Contains('.')&&!raw.Contains('e')&&!raw.Contains('E')) return UploadIdentityValue.RawComparison(atom);
            double n=double.Parse(raw,CultureInfo.InvariantCulture);if(n==0) return "";if(!double.IsFinite(n)) throw new ValidationPlanRejected("import_fingerprint_review_required");
            string roundtrip=Math.Abs(n).ToString("R",CultureInfo.InvariantCulture).ToLowerInvariant();var parts=roundtrip.Split('e');
            string mantissa=parts[0];int exponent=parts.Length==2?int.Parse(parts[1],CultureInfo.InvariantCulture):0;
            int point=mantissa.IndexOf('.');if(point<0) point=mantissa.Length;
            string digits=mantissa.Replace(".","",StringComparison.Ordinal);int leading=digits.Length-digits.TrimStart('0').Length;
            int adjusted=point-leading-1+exponent;digits=digits.TrimStart('0').TrimEnd('0');
            string result;
            if(adjusted is <-4 or >=16)
                result=digits[0]+(digits.Length>1?"."+digits[1..]:"")+"e"+(adjusted<0?"-":"+")+Math.Abs(adjusted).ToString("D2",CultureInfo.InvariantCulture);
            else
            {
                int split=adjusted+1;
                result=split<=0?"0."+new string('0',-split)+digits:split>=digits.Length?digits+new string('0',split-digits.Length)+".0":digits.Insert(split,".");
            }
            return (n<0?"-":"")+result;
        }
        var fields=values.Select(p=>(p.Key,Value:Text(p.Value))).Where(p=>p.Value.Length>0)
            .OrderBy(p=>p.Key,Comparer<string>.Create((a,b)=>CompareRunes(a,b)));
        return UploadIdentityValue.Hash("["+string.Join(",",fields.Select(p=>"["+Quote(p.Key)+","+Quote(p.Value)+"]"))+"]");
    }
    private static int CompareRunes(string a,string b)
    {
        var x=a.EnumerateRunes().GetEnumerator();var y=b.EnumerateRunes().GetEnumerator();
        while(true) {bool p=x.MoveNext(),q=y.MoveNext();if(!p||!q) return p.CompareTo(q);int c=x.Current.Value.CompareTo(y.Current.Value);if(c!=0) return c;}
    }
}
