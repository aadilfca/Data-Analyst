using System.Globalization;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Workbooks;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Uploads;

public sealed class UploadIdentityService(UploadCoverageService coverage,PostgresReadSnapshot snapshots,IOptions<UploadIdentityOptions> options)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    private const string Notes="Ledger Source Notes";
    private static readonly HashSet<string> LineAliases=["line id","line_id","line no","line_no","line number","line_number",
        "line item","line_item","item no","item_no","item number","item_number"];
    private sealed record Stored(IReadOnlyList<IdentityStoredRow> Rows,string Key,Dictionary<string,List<long>> Identities);
    private static long Cost(IReadOnlyDictionary<string,IdentityAtom> values)=>values.Sum(v=>128L+2L*(v.Key.Length+(v.Value.Text?.Length??0)));
    public async Task<IdentityResult> CheckAsync(VerifiedSession? session,int file,IdentityRequest input,CancellationToken ct=default)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(input.Coverage?.Footing?.Validation?.Source is null||input.ExpectedIdentityKey is not null&&input.ExpectedIdentityKey.Length!=64
            ||input.Page is <1 or >1048576||input.PageSize is <1 or >100) return new("invalid",Code:"invalid_identity_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            IdentityProfile? profile=null;
            var incoming=new Dictionary<int,IdentitySourceRow>();long bytes=0;
            var source=input.Coverage.Footing.Validation.Source;
            var result=await coverage.ReadCheckedAsync(session,file,input.Coverage,(template,context,maps)=>
            {
                profile=Profile(template,input.Coverage.LedgerAccountCode);
                return observed=>
                {
                    var row=observed.Row;
                    if(observed.Kind=="transaction"&&observed.Eligible)
                    {
                        if(incoming.Count>=options.Value.MaximumRows) throw new IdentityReview("comparison_capacity_review_required");
                        var values=maps.ToDictionary(m=>m.StorageKey,m=>UploadIdentityValue.FromCell(m.SourcePosition is >0 and var position&&position<=row.Cells.Count
                            ?row.Cells[position-1]:new(row.Number,m.SourcePosition??0,WorkbookCellKind.Blank),m.Type),StringComparer.Ordinal);
                        string? date=null;var primary=maps.SingleOrDefault(m=>m.StorageKey==profile.DateKey);
                        if(primary?.SourcePosition is >0 and var datePosition&&datePosition<=row.Cells.Count)
                            date=UploadDateParser.Parse(row.Cells[datePosition-1],source.DateDelimiter??"/",(source.DateOrder??"DMY").ToUpperInvariant())?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture);
                        bytes+=Cost(values);if(bytes>options.Value.MaximumComparisonBytes) throw new IdentityReview("comparison_capacity_review_required");
                        incoming.Add(row.Number,new(row.Number,values,date));
                    }
                    else if(observed.Kind=="continuation"&&!observed.HasErrors&&observed.ParentRow is {} parent&&incoming.TryGetValue(parent,out var item))
                    {
                        var description=maps.Single(m=>m.ColumnId==template.Ledger["description"]);
                        var cell=row.Cells[description.SourcePosition!.Value-1];
                        var values=new Dictionary<string,IdentityAtom>(item.Values,StringComparer.Ordinal);
                        string note=cell.Text??"";string joined=values.TryGetValue(Notes,out var prior)?prior.Text+"\n"+note:note;
                        values[Notes]=new("text",joined);bytes+=128+note.Length*2L;
                        if(bytes>options.Value.MaximumComparisonBytes) throw new IdentityReview("comparison_capacity_review_required");
                        incoming[parent]=item with {Values=values};
                    }
                };
            },async(proof,token)=>
            {
                var rows=incoming.Values.ToArray();
                if(!proof.CoverageChecksPassed) return Build(profile!,rows,[],input,proof,"",new Dictionary<string,List<long>>(),options.Value,token,sourceFailed:true);
                var identities=Identities(profile!,rows);
                var stored=await ReadStoredAsync(session!,profile!,input,proof,rows,identities,bytes,token);
                var output=Build(profile!,rows,stored.Rows,input,proof,stored.Key,stored.Identities,options.Value,token);
                UploadPreparationService.JsonSize(output,options.Value.MaximumResponseBytes);
                return output;
            },deadline.Token);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            var value=result.Value!;UploadPreparationService.JsonSize(value,options.Value.MaximumResponseBytes);
            return value.Status=="confirmation_changed"?new("conflict",value,"identity_check_changed")
                :value.Coverage.Footing.Status=="invalid_confirmation"?new("invalid",value,"variance_confirmation_not_applicable"):new("success",value);
        }
        catch(IdentityReview review) {return new("review",Code:review.Code);}
        catch(PostgresException error) when(error.SqlState is "42P01" or "42703") {return new("review",Code:"canonical_store_review_required");}
        catch(OperationCanceledException) when(!caller.IsCancellationRequested&&deadline.IsCancellationRequested) {return new("timeout",Code:"identity_timed_out");}
        catch(UploadPreparationService.InvalidPreparation) {return new("review",Code:"identity_response_limit");}
        catch(System.Text.RegularExpressions.RegexMatchTimeoutException) {return new("review",Code:"identity_value_review_required");}
        catch(JsonException) {return new("review",Code:"stored_values_review_required");}
        finally {requests.Release();}
    }

    private static IdentityProfile Profile(TemplateDefinition template,string? account)
    {
        if(template.Origin!="native"||template.Columns.Any(c=>c.Review)) throw new IdentityReview("identity_binding_review_required");
        // Native metadata has no immutable Line ID/Rate-role bindings yet. Do
        // not certify label-derived semantics or introduce a schema implicitly.
        if(template.Columns.Any(c=>LineAliases.Contains(UploadNumericParser.Trim(c.Name).ToLowerInvariant()))) throw new IdentityReview("line_binding_review_required");
        if(template.RateSource is "auto" or "derived") throw new IdentityReview("derived_rate_binding_review_required");
        var document=template.Columns.SingleOrDefault(c=>c.Role=="document_id")??template.Columns.First();
        var date=template.Purpose=="account_ledger"?template.Columns.SingleOrDefault(c=>c.Id==template.Ledger.GetValueOrDefault("date"))
            :template.Columns.FirstOrDefault(c=>c.Type=="Date");
        if(template.Columns.SingleOrDefault(c=>c.Role=="document_date") is {} configured&&date?.Id!=configured.Id)
            throw new IdentityReview("comparison_date_binding_review_required");
        int? description=template.Purpose=="account_ledger"?template.Ledger.GetValueOrDefault("description"):null;
        return new(template.Id,template.Columns.Select(c=>new IdentityColumn(c.StorageKey,c.Type,TenantTemplateService.EffectiveComparison(c,template),c.Id==description)).ToArray(),
            document.StorageKey,date?.StorageKey,AccountCode:template.Purpose=="account_ledger"?account?.Trim():null);
    }
    private static string[] Identities(IdentityProfile profile,IReadOnlyList<IdentitySourceRow> rows)
    {
        var occurrences=new Dictionary<string,int>(StringComparer.Ordinal);
        return rows.Select(row=>
        {
            string key=UploadIdentityValue.Group(profile,row.Values)+"|"+UploadIdentityValue.SourceFingerprint(profile,row.Values,includedOnly:true);
            int occurrence=occurrences.GetValueOrDefault(key)+1;occurrences[key]=occurrence;
            return UploadIdentityValue.SourceIdentity(profile,row.Values,occurrence);
        }).ToArray();
    }
    private Task<Stored> ReadStoredAsync(VerifiedSession session,IdentityProfile profile,IdentityRequest input,CoverageOutput proof,
        IReadOnlyList<IdentitySourceRow> incoming,string[] identities,long retained,CancellationToken ct)=>snapshots.ExecuteAsync(async(db,token)=>
    {
        var result=new List<IdentityStoredRow>();var guards=new StringBuilder();long last=0,bytes=retained;
        bool complete=input.Coverage.Footing.Validation.ComparisonMode=="replace_range";
        string[] groups=incoming.Select(r=>UploadIdentityValue.Group(profile,r.Values)).Distinct(StringComparer.Ordinal).ToArray();
        var source=input.Coverage.Footing.Validation.Source;var period=proof.DeclaredPeriod;
        while(true)
        {
            await using var command=db.CreateCommand("""
                SELECT cd.id,CASE WHEN octet_length(cd.row_data::text)<=$8 THEN cd.row_data::text END,
                    COALESCE(cd.version,1),cd.document_group_id,cd.source_identity,cd.source_row_number,cd.source_order_upload_id,cd.extract_date::text,cd.change_detection_status
                FROM public.client_data cd
                WHERE cd.id>$1 AND cd.client_id=$2 AND cd.template_id=$3 AND cd.is_deleted IS FALSE
                  AND EXISTS(SELECT 1 FROM public.platform_tenant_client_homes h WHERE h.client_id=cd.client_id AND h.tenant_id=$4)
                  AND ($5::text IS NULL OR EXISTS(SELECT 1 FROM public.uploaded_files u WHERE u.id=cd.upload_id AND u.ledger_account_code=$5))
                  AND ($6::date IS NULL OR cd.extract_date BETWEEN $6::date AND $7::date OR ($5::text IS NULL AND cd.document_group_id=ANY($9)))
                ORDER BY cd.id LIMIT 1000
                """);
            command.Parameters.AddWithValue(last);command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);command.Parameters.AddWithValue(session.TenantId!.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)profile.AccountCode??DBNull.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)(complete||profile.AccountCode is not null?period?.Start:null)??DBNull.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Text,(object?)(complete||profile.AccountCode is not null?period?.End:null)??DBNull.Value);
            command.Parameters.AddWithValue(options.Value.MaximumComparisonBytes);command.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,groups);
            int read=0;await using var reader=await command.ExecuteReaderAsync(token);
            while(await reader.ReadAsync(token))
            {
                token.ThrowIfCancellationRequested();if(result.Count>=options.Value.MaximumRows||reader.IsDBNull(1)) throw new IdentityReview("comparison_capacity_review_required");
                if(!reader.IsDBNull(8)&&reader.GetString(8)=="deleted") throw new IdentityReview("canonical_status_review_required");
                long id=reader.GetInt32(0);string json=reader.GetString(1);var values=UploadIdentityValue.FromJson(json);
                if(values.Keys.Except(profile.Columns.Select(c=>c.Key).Append(Notes),StringComparer.Ordinal).Any()) throw new IdentityReview("stored_binding_review_required");
                bytes+=Cost(values);if(bytes>options.Value.MaximumComparisonBytes) throw new IdentityReview("comparison_capacity_review_required");
                string? Text(int field)=>reader.IsDBNull(field)?null:reader.GetString(field);
                var row=new IdentityStoredRow(id,values,Text(3),Text(4),reader.GetInt32(2),reader.IsDBNull(5)?null:reader.GetInt32(5),reader.IsDBNull(6)?null:reader.GetInt32(6),Text(7));
                result.Add(row);last=id;read++;
                guards.Append(UploadIdentityValue.Hash(JsonSerializer.Serialize(new {row.Id,json,row.Version,row.DocumentGroup,row.Identity,row.SourceRow,row.OrderUpload,row.PrimaryDate})));
            }
            if(read<1000) break;
        }
        var active=new Dictionary<string,List<long>>(StringComparer.Ordinal);
        if(identities.Length>0)
        {
            // Include collisions outside the comparison period/account; they
            // cannot disappear just because this preview reads a narrower scope.
            await using var command=db.CreateCommand("""
                SELECT id,source_identity FROM public.client_data
                WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE AND COALESCE(change_detection_status,'')<>'deleted'
                  AND source_identity=ANY($3) ORDER BY id LIMIT $4
                """);
            command.Parameters.AddWithValue(source.ClientId);command.Parameters.AddWithValue(source.TemplateId);
            command.Parameters.AddWithValue(NpgsqlDbType.Array|NpgsqlDbType.Text,identities.Distinct(StringComparer.Ordinal).ToArray());command.Parameters.AddWithValue(options.Value.MaximumRows+1);
            await using var reader=await command.ExecuteReaderAsync(token);int count=0;
            while(await reader.ReadAsync(token))
            {
                if(++count>options.Value.MaximumRows) throw new IdentityReview("comparison_capacity_review_required");
                string identity=reader.GetString(1);long id=reader.GetInt32(0);
                if(!active.TryGetValue(identity,out var ids)) active[identity]=ids=[];ids.Add(id);
                guards.Append(UploadIdentityValue.Hash(id.ToString(CultureInfo.InvariantCulture)+":"+identity));
            }
        }
        return new Stored(result,UploadIdentityValue.Hash(guards.ToString()),active);
    },ct);

    // Pure occurrence matching, also used by byte/hash and matching oracles.
    // Unmatched rows are left for P23: this method never guesses corrections.
    public static IdentityOutput Build(IdentityProfile profile,IReadOnlyList<IdentitySourceRow> incoming,IReadOnlyList<IdentityStoredRow> existing,
        IdentityRequest request,CoverageOutput proof,string storeKey,IReadOnlyDictionary<string,List<long>> collisions,
        UploadIdentityOptions limits,CancellationToken ct=default,bool sourceFailed=false)
    {
        string delimiter=request.Coverage.Footing.Validation.Source.DateDelimiter??"/",order=(request.Coverage.Footing.Validation.Source.DateOrder??"DMY").ToUpperInvariant();
        var issues=new List<IdentityIssue>();int issueCount=0;long issueBytes=0;
        void Issue(int row,string kind,string message)
        {
            issueCount++;if(issues.Count>=limits.MaximumIssueDetails) return;
            var issue=new IdentityIssue(row,kind,message);
            try {issueBytes+=UploadPreparationService.JsonSize(issue,limits.MaximumResponseBytes/8-issueBytes);issues.Add(issue);} catch(UploadPreparationService.InvalidPreparation) { }
        }
        if(profile.LineKey is not null&&!profile.Columns.Any(c=>c.Key==profile.LineKey&&c.Compare)) throw new IdentityReview("line_comparison_review_required");
        string[] identities=sourceFailed?[]:Identities(profile,incoming);
        var sourceGroups=incoming.Select(r=>UploadIdentityValue.Group(profile,r.Values)).ToArray();
        var currentGroups=existing.Select(r=>r.DocumentGroup??UploadIdentityValue.Group(profile,r.Values)).ToArray();
        long bytes=incoming.Sum(r=>Cost(r.Values))+existing.Sum(r=>Cost(r.Values));
        if(bytes>limits.MaximumComparisonBytes||incoming.Count>limits.MaximumRows||existing.Count>limits.MaximumRows) throw new IdentityReview("comparison_capacity_review_required");
        string[] Keys(int count,Func<int,string> value)
        {
            var keys=new string[count];for(int index=0;index<count;index++)
            {ct.ThrowIfCancellationRequested();string key=value(index);bytes+=2L*key.Length;
                if(bytes>limits.MaximumComparisonBytes) throw new IdentityReview("comparison_capacity_review_required");keys[index]=key;}
            return keys;
        }
        var exactNew=Keys(incoming.Count,i=>UploadIdentityValue.Fingerprint(profile,incoming[i].Values,incoming[i].PrimaryDate,delimiter,order));
        var exactOld=Keys(existing.Count,i=>UploadIdentityValue.Fingerprint(profile,existing[i].Values,existing[i].PrimaryDate,delimiter,order,stored:true));
        var equivalentNew=Keys(incoming.Count,i=>UploadIdentityValue.Fingerprint(profile,incoming[i].Values,incoming[i].PrimaryDate,delimiter,order,equivalent:true));
        var equivalentOld=Keys(existing.Count,i=>UploadIdentityValue.Fingerprint(profile,existing[i].Values,existing[i].PrimaryDate,delimiter,order,stored:true,equivalent:true));
        var matched=new Dictionary<int,int>();var available=Enumerable.Range(0,existing.Count).ToHashSet();
        if(!sourceFailed)
        foreach(bool equivalent in new[]{false,true})
        {
            var buckets=new Dictionary<(string,string),Queue<int>>();
            foreach(int index in available.OrderBy(i=>existing[i].Id))
            {
                var matchKey=(currentGroups[index],(equivalent?equivalentOld:exactOld)[index]);
                if(!buckets.TryGetValue(matchKey,out var queue)) buckets[matchKey]=queue=new();queue.Enqueue(index);
            }
            for(int index=0;index<incoming.Count;index++)
            {
                ct.ThrowIfCancellationRequested();if(matched.ContainsKey(index)) continue;
                if(buckets.TryGetValue((sourceGroups[index],(equivalent?equivalentNew:exactNew)[index]),out var queue)&&queue.TryDequeue(out var old))
                {matched[index]=old;available.Remove(old);}
            }
        }
        int repeatedGroups=0,repeatedRows=0,duplicateLineRows=0;
        if(!sourceFailed)
        foreach(var group in incoming.Select((row,index)=>new {row,index,Key=UploadIdentityValue.SourceIdentity(profile,row.Values)}).GroupBy(x=>x.Key))
        {
            int count=group.Count();if(count<2) continue;repeatedGroups++;repeatedRows+=count;
            if(profile.LineKey is null) continue;
            duplicateLineRows+=count;foreach(var item in group) Issue(item.row.Row,"duplicate_line_id","Repeated explicit Line ID in this document. Correct the source line identifiers.");
        }
        foreach(var group in existing.Where(r=>!string.IsNullOrEmpty(r.Identity)).GroupBy(r=>r.Identity))
            if(group.Count()>1) Issue(0,"stored_identity_collision","Existing active identities require review; no rows were merged or repaired.");
        int informational=0,positions=0;
        foreach(var pair in matched)
        {
            var old=existing[pair.Value];var row=incoming[pair.Key];
            if(string.IsNullOrEmpty(old.Identity)) Issue(row.Row,"stored_identity_missing","Stored identity needs review; it will not be regenerated automatically.");
            if(profile.Columns.Where(c=>!c.Compare).Any(c=>UploadIdentityValue.RawComparison(old.Values.GetValueOrDefault(c.Key,new("blank")))
                !=UploadIdentityValue.RawComparison(row.Values.GetValueOrDefault(c.Key,new("blank"))))) informational++;
            if(old.SourceRow!=row.Row||old.OrderUpload is null) positions++;
        }
        for(int index=0;index<identities.Length;index++)
            if(collisions.TryGetValue(identities[index],out var ids)&&ids.Any(id=>!matched.TryGetValue(index,out var prior)||existing[prior].Id!=id))
                Issue(incoming[index].Row,"active_identity_collision","This proposed identity is already held by a different active row. Review it before any write.");
        bool complete=request.Coverage.Footing.Validation.ComparisonMode=="replace_range";
        var counts=new IdentityCounts(incoming.Count,existing.Count,matched.Count,incoming.Count-matched.Count,available.Count,
            repeatedGroups,repeatedRows,duplicateLineRows,informational,positions);
        string key=UploadIdentityValue.Hash(JsonSerializer.Serialize(new {Policy="Identity-v2-review",proof.CoverageKey,storeKey,profile,
            Source=identities,Matches=matched.OrderBy(m=>m.Key).Select(m=>new {Row=incoming[m.Key].Row,existing[m.Value].Id,existing[m.Value].Identity}),counts}));
        bool stale=request.ExpectedIdentityKey is not null&&request.ExpectedIdentityKey!=key||proof.Status=="confirmation_changed";
        bool passed=!sourceFailed&&proof.CoverageChecksPassed&&issueCount==0&&!stale;
        bool unchanged=passed&&counts.UnmatchedIncoming==0&&(!complete||counts.UnmatchedExisting==0);
        bool noOp=unchanged&&informational==0&&positions==0&&profile.AccountCode is null;
        string status=stale?"confirmation_changed":sourceFailed?"source_checks_failed":issueCount>0?"identity_checks_failed":!unchanged?"unmatched_rows"
            :noOp?"no_changes":"row_metadata_review_required";
        var page=new List<IdentityRowCheck>();long pageBytes=0;long offset=(long)(request.Page-1)*request.PageSize;
        if(!sourceFailed) for(int index=0;index<incoming.Count;index++)
        {
            if(index<offset||index>=offset+request.PageSize) continue;
            bool found=matched.TryGetValue(index,out int prior);
            var item=new IdentityRowCheck(incoming[index].Row,sourceGroups[index],found?existing[prior].Identity??"":identities[index],found?existing[prior].Id:null,found?"unchanged":"unmatched");
            try {pageBytes+=UploadPreparationService.JsonSize(item,limits.MaximumResponseBytes/4-pageBytes);page.Add(item);} catch(UploadPreparationService.InvalidPreparation) { }
        }
        var warnings=new List<string>{"Identity evidence is read-only and bound to the captured store snapshot. P23 corrections, P24 plans and actual import remain gated."};
        if(profile.AccountCode is not null) warnings.Add("Unchanged transaction rows do not certify unchanged ledger balance/coverage evidence.");
        if(informational>0||positions>0) warnings.Add("Unchanged financial values still require unchecked-field or source-order review.");
        return new(status,proof,key,storeKey,counts,passed,unchanged,noOp,false,request.Page,request.PageSize,page,issueCount,issues,
            issueCount>issues.Count||(!sourceFailed&&page.Count<Math.Min(request.PageSize,Math.Max(0L,incoming.Count-offset))),warnings);
    }
}
