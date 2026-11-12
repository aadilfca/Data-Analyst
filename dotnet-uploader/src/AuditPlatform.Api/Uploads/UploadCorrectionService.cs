using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Uploads;

// Review only: actions are disposable evidence. P24/P25 must independently
// establish a fresh signed plan and atomically preserve current/history rows.
public sealed class UploadCorrectionService(UploadIdentityService identity,IOptions<UploadCorrectionOptions> options)
{
    private readonly SemaphoreSlim requests=new(options.Value.MaximumConcurrentRequests);
    public Task<CorrectionResult> CheckAsync(VerifiedSession? session,int file,CorrectionRequest input,CancellationToken ct=default)=>RunAsync(session,file,input,null,ct);
    internal async Task<(CorrectionResult Result,CorrectionEvidence? Evidence)> CaptureAsync(VerifiedSession? session,int file,CorrectionRequest input,CancellationToken ct)
    {
        CorrectionEvidence? evidence=null;
        var result=await RunAsync(session,file,input,value=>evidence=value,ct);
        return (result,result.Status=="success"?evidence:null);
    }
    private async Task<CorrectionResult> RunAsync(VerifiedSession? session,int file,CorrectionRequest input,Action<CorrectionEvidence>? capture,CancellationToken ct)
    {
        if(!options.Value.Enabled) return new("unavailable");
        if(input.Coverage?.Footing?.Validation?.Source is null||input.ExpectedCorrectionKey is not null&&input.ExpectedCorrectionKey.Length!=64
            ||input.Page is <1 or >1048576||input.PageSize is <1 or >100) return new("invalid",Code:"invalid_correction_request");
        if(!await requests.WaitAsync(0,ct)) return new("busy");
        var caller=ct;using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        try
        {
            CorrectionEvidence? evidence=null;
            var result=await identity.ReadCheckedAsync(session,file,new(input.Coverage),(context,token)=>
                {
                    IReadOnlyList<CorrectionRow> actions=[];
                    var output=Build(context.Profile,context.Incoming,context.Existing,input,context.Coverage,
                        context.StoreKey,context.Collisions,context.IncompleteGroups,new(RequiresBindingReview:true),options.Value,token,context.SourceFailed,
                        capture is null?null:rows=>actions=rows.ToArray());
                    if(capture is not null) evidence=new(output,context.Profile,context.Incoming,context.Existing,actions);
                    return Task.FromResult(output);
                },
                deadline.Token,corrections:true);
            if(result.Status!="success") return new(result.Status,Code:result.Code);
            var output=result.Output!;UploadPreparationService.JsonSize(output,options.Value.MaximumResponseBytes);
            if(evidence is not null) capture?.Invoke(evidence);
            return output.Status=="confirmation_changed"?new("conflict",output,"correction_check_changed")
                :output.Coverage.Footing.Status=="invalid_confirmation"?new("invalid",output,"variance_confirmation_not_applicable"):new("success",output);
        }
        catch(IdentityReview review) {return new("review",Code:review.Code);}
        catch(UploadPreparationService.InvalidPreparation) {return new("review",Code:"correction_response_limit");}
        catch(OperationCanceledException) when(!caller.IsCancellationRequested&&deadline.IsCancellationRequested)
        {return new("timeout",Code:"correction_timed_out");}
        finally {requests.Release();}
    }

    public static CorrectionOutput Build(IdentityProfile profile,IReadOnlyList<IdentitySourceRow> incoming,
        IReadOnlyList<IdentityStoredRow> existing,CorrectionRequest request,CoverageOutput proof,string storeKey,
        IReadOnlyDictionary<string,List<long>> collisions,IReadOnlySet<string> incompleteGroups,CorrectionBindings bindings,
        UploadCorrectionOptions limits,CancellationToken ct=default,bool sourceFailed=false,Action<IReadOnlyList<CorrectionRow>>? observe=null)
    {
        var source=request.Coverage.Footing.Validation.Source;
        string delimiter=source.DateDelimiter??"/",order=(source.DateOrder??"DMY").ToUpperInvariant();
        bool complete=request.Coverage.Footing.Validation.ComparisonMode=="replace_range";
        var issues=new List<CorrectionIssue>();int issueCount=0;long issueBytes=0,bytes=0;
        void Charge(long cost)
        {bytes+=cost;if(bytes>limits.MaximumComparisonBytes) throw new IdentityReview("comparison_capacity_review_required");}
        void Issue(string kind,string group,int added=0,int prior=0,IReadOnlyList<string>? fields=null)
        {
            issueCount++;if(issues.Count>=limits.MaximumIssueDetails) return;
            var issue=new CorrectionIssue(kind,group,added,prior,fields??[]);
            try {issueBytes+=UploadPreparationService.JsonSize(issue,limits.MaximumResponseBytes/8-issueBytes);issues.Add(issue);}
            catch(UploadPreparationService.InvalidPreparation) { }
        }
        if(incoming.Count>limits.MaximumRows||existing.Count>limits.MaximumRows) throw new IdentityReview("comparison_capacity_review_required");
        if(profile.LineKey is not null&&!profile.Columns.Any(c=>c.Key==profile.LineKey&&c.Compare)) throw new IdentityReview("line_comparison_review_required");
        foreach(string? bindingKey in new[]{bindings.AccountKey,bindings.DebitKey,bindings.CreditKey})
            if(bindingKey is not null&&!profile.Columns.Any(c=>c.Key==bindingKey&&c.Compare)) throw new IdentityReview("correction_binding_review_required");
        if(bindings.AccountKey is not null&&(bindings.DebitKey is null||bindings.CreditKey is null))
            throw new IdentityReview("correction_binding_review_required");
        var identityLimits=new UploadIdentityOptions{MaximumRows=limits.MaximumRows,MaximumComparisonBytes=(int)limits.MaximumComparisonBytes};
        var match=UploadIdentityService.Match(profile,incoming,existing,delimiter,order,identityLimits,ct,sourceFailed);
        var matched=new Dictionary<int,int>(match.Matched);var changed=new Dictionary<int,int>();
        var remainingNew=Enumerable.Range(0,incoming.Count).Where(i=>!matched.ContainsKey(i)).ToHashSet();
        var remainingOld=match.UnmatchedExisting.ToHashSet();
        IReadOnlyDictionary<string,string> Fields(IReadOnlyDictionary<string,IdentityAtom> values,string? date,bool stored)
        {
            ct.ThrowIfCancellationRequested();Charge(values.Sum(v=>128L+2L*(v.Key.Length+(v.Value.Text?.Length??0))));
            var fields=UploadIdentityValue.ComparedFields(profile,values,date,delimiter,order,stored);
            Charge(fields.Sum(v=>128L+2L*(v.Key.Length+v.Value.Length)));return fields;
        }
        var newFields=sourceFailed?[]:incoming.Select(r=>Fields(r.Values,r.PrimaryDate,false)).ToArray();
        var oldFields=sourceFailed?[]:existing.Select(r=>Fields(r.Values,r.PrimaryDate,true)).ToArray();
        string[] ChangedFields(int added,int prior)=>newFields[added].Keys.Union(oldFields[prior].Keys,StringComparer.Ordinal)
            .Where(k=>newFields[added].GetValueOrDefault(k)!=oldFields[prior].GetValueOrDefault(k)).Order(StringComparer.Ordinal).ToArray();
        void Pair(int added,int prior)
        {changed.Add(added,prior);remainingNew.Remove(added);remainingOld.Remove(prior);}
        var numeric=profile.Columns.Where(c=>c.Type=="Numeric").Select(c=>c.Key).ToHashSet(StringComparer.Ordinal);
        string Anchor(IReadOnlyDictionary<string,string> fields)
        {string value=UploadIdentityValue.FieldKey(fields.Where(v=>!numeric.Contains(v.Key)));Charge(2L*value.Length+64);return value;}
        string Account(IReadOnlyDictionary<string,IdentityAtom> values)
        {
            ct.ThrowIfCancellationRequested();
            string code=UploadIdentityValue.LineKey(values.GetValueOrDefault(bindings.AccountKey!,new("blank")));
            return JsonSerializer.Serialize(new {code,Debit=UploadIdentityValue.Movement(values.GetValueOrDefault(bindings.DebitKey!,new("blank"))),
                Credit=UploadIdentityValue.Movement(values.GetValueOrDefault(bindings.CreditKey!,new("blank")))});
        }
        string[] PopulationFields(int[] uploaded,int[] current)
        {
            var fields=uploaded.SelectMany(i=>newFields[i].Keys).Concat(current.SelectMany(i=>oldFields[i].Keys)).Distinct(StringComparer.Ordinal);
            return fields.Where(field=>UploadIdentityValue.FieldKey(uploaded.Select(i=>newFields[i].GetValueOrDefault(field,""))
                .GroupBy(v=>v,StringComparer.Ordinal).Select(g=>new KeyValuePair<string,string>(g.Key,g.Count().ToString(System.Globalization.CultureInfo.InvariantCulture))))
                !=UploadIdentityValue.FieldKey(current.Select(i=>oldFields[i].GetValueOrDefault(field,""))
                    .GroupBy(v=>v,StringComparer.Ordinal).Select(g=>new KeyValuePair<string,string>(g.Key,g.Count().ToString(System.Globalization.CultureInfo.InvariantCulture)))))
                .Order(StringComparer.Ordinal).Take(8).ToArray();
        }
        // Buckets are built once per pass, avoiding a cross-product of repeated lines.
        void UniquePairs(int[] uploaded,int[] current,Func<int,string> newKey,Func<int,string> oldKey)
        {
            var oldBuckets=current.GroupBy(oldKey,StringComparer.Ordinal).ToDictionary(g=>g.Key,g=>g.ToArray(),StringComparer.Ordinal);
            var newBuckets=uploaded.GroupBy(newKey,StringComparer.Ordinal).ToDictionary(g=>g.Key,g=>g.ToArray(),StringComparer.Ordinal);
            foreach(var bucket in newBuckets)
            {ct.ThrowIfCancellationRequested();if(bucket.Value.Length==1&&oldBuckets.TryGetValue(bucket.Key,out var old)&&old.Length==1) Pair(bucket.Value[0],old[0]);}
        }
        if(!sourceFailed)
        {
            foreach(var group in existing.GroupBy(r=>r.Id)) if(group.Count()>1) Issue("stored_id_collision","",prior:group.Count());
            foreach(var group in existing.Where(r=>!string.IsNullOrEmpty(r.Identity)).GroupBy(r=>r.Identity,StringComparer.Ordinal))
                if(group.Count()>1) Issue("stored_identity_collision","",prior:group.Count());
            if(profile.LineKey is not null)
            {
                string NewLine(int i)=>UploadIdentityValue.LineKey(incoming[i].Values.GetValueOrDefault(profile.LineKey,new("blank")));
                string OldLine(int i)=>UploadIdentityValue.LineKey(existing[i].Values.GetValueOrDefault(profile.LineKey,new("blank")));
                foreach(var g in Enumerable.Range(0,incoming.Count).GroupBy(i=>(match.IncomingGroups[i],Line:NewLine(i))))
                    if(g.Count()>1) Issue("duplicate_line_id",g.Key.Item1,g.Count());
                foreach(var g in Enumerable.Range(0,existing.Count).GroupBy(i=>(match.ExistingGroups[i],Line:OldLine(i))))
                    if(g.Count()>1) Issue("stored_line_id_collision",g.Key.Item1,prior:g.Count());
            }
            var uploadedGroups=Enumerable.Range(0,incoming.Count).GroupBy(i=>match.IncomingGroups[i],StringComparer.Ordinal)
                .ToDictionary(g=>g.Key,g=>g.ToArray(),StringComparer.Ordinal);
            var currentGroups=Enumerable.Range(0,existing.Count).GroupBy(i=>match.ExistingGroups[i],StringComparer.Ordinal)
                .ToDictionary(g=>g.Key,g=>g.ToArray(),StringComparer.Ordinal);
            foreach(string group in uploadedGroups.Keys.Union(currentGroups.Keys,StringComparer.Ordinal).Order(StringComparer.Ordinal))
            {
                ct.ThrowIfCancellationRequested();int[] uploaded=uploadedGroups.GetValueOrDefault(group,[]).Where(remainingNew.Contains).ToArray();
                int[] current=currentGroups.GetValueOrDefault(group,[]).Where(remainingOld.Contains).ToArray();
                if(uploaded.Length==0||current.Length==0) continue;
                if(profile.LineKey is not null)
                {
                    UniquePairs(uploaded,current,i=>UploadIdentityValue.LineKey(incoming[i].Values.GetValueOrDefault(profile.LineKey,new("blank"))),
                        i=>UploadIdentityValue.LineKey(existing[i].Values.GetValueOrDefault(profile.LineKey,new("blank"))));
                    continue; // Different explicit identifiers never inherit another row's history.
                }
                UniquePairs(uploaded,current,i=>Anchor(newFields[i]),i=>Anchor(oldFields[i]));
                uploaded=uploaded.Where(remainingNew.Contains).ToArray();current=current.Where(remainingOld.Contains).ToArray();
                if(uploaded.Length==0||current.Length==0) continue;
                if(bindings.RequiresBindingReview)
                {Issue("correction_binding_review_required",group,uploaded.Length,current.Length);continue;}
                if(bindings.AccountKey is not null)
                {
                    UniquePairs(uploaded,current,i=>Account(incoming[i].Values),i=>Account(existing[i].Values));
                    if(!complete)
                    {
                        var oldBuckets=current.Where(remainingOld.Contains).GroupBy(i=>Account(existing[i].Values)).ToDictionary(g=>g.Key,g=>g.ToArray(),StringComparer.Ordinal);
                        foreach(var bucket in uploaded.Where(remainingNew.Contains).GroupBy(i=>Account(incoming[i].Values)))
                            if(oldBuckets.TryGetValue(bucket.Key,out var oldBucket))
                            {
                                Issue("ambiguous_correction",group,bucket.Count(),oldBucket.Length,PopulationFields(bucket.ToArray(),oldBucket));
                            }
                    }
                }
                else if(bindings.AllowSingleFallback&&uploaded.Length==1&&current.Length==1) Pair(uploaded[0],current[0]);
                else if(!complete) Issue("ambiguous_correction",group,uploaded.Length,current.Length,PopulationFields(uploaded,current));
            }
        }
        var rows=new List<CorrectionRow>();var changedGroups=new HashSet<string>(StringComparer.Ordinal);int information=0,positions=0;
        void Add(CorrectionRow row) {Charge(256L+2L*(row.DocumentGroup.Length+row.Identity.Length)+row.ChangedFields.Sum(f=>2L*f.Length+32));rows.Add(row);}
        if(!sourceFailed)
        {
            for(int i=0;i<incoming.Count;i++)
            {
                ct.ThrowIfCancellationRequested();string group=match.IncomingGroups[i];
                bool correction=changed.TryGetValue(i,out int oldIndex);bool same=matched.TryGetValue(i,out int sameIndex);
                var old=correction?existing[oldIndex]:same?existing[sameIndex]:null;
                string identityValue=old?.Identity??match.Identities[i];
                if(old is not null&&(string.IsNullOrEmpty(old.Identity)||old.Version<1)) Issue("stored_identity_missing",group);
                if(collisions.TryGetValue(match.Identities[i],out var ids)&&ids.Any(id=>old?.Id!=id)) Issue("active_identity_collision",group);
                if(same)
                {
                    if(profile.Columns.Where(c=>!c.Compare).Any(c=>UploadIdentityValue.RawComparison(old!.Values.GetValueOrDefault(c.Key,new("blank")))
                        !=UploadIdentityValue.RawComparison(incoming[i].Values.GetValueOrDefault(c.Key,new("blank"))))) information++;
                    if(old!.SourceRow!=incoming[i].Row||old.OrderUpload is null) positions++;
                }
                if(correction||!same) changedGroups.Add(group);
                Add(new(correction?"changed":same?"unchanged":"added",incoming[i].Row,old?.Id,group,identityValue,old?.Version,correction?ChangedFields(i,oldIndex):[]));
            }
            foreach(int prior in remainingOld.OrderBy(i=>existing[i].Id))
            {
                ct.ThrowIfCancellationRequested();var old=existing[prior];string group=match.ExistingGroups[prior];
                if(complete)
                {
                    changedGroups.Add(group);
                    if(string.IsNullOrEmpty(old.Identity)||old.Version<1) Issue("stored_identity_missing",group);
                    Add(new("removed",null,old.Id,group,old.Identity??"",old.Version,[]));
                }
            }
            foreach(string group in incompleteGroups.Order(StringComparer.Ordinal))
                if(changedGroups.Contains(group)||profile.AccountCode is not null&&match.IncomingGroups.Contains(group,StringComparer.Ordinal))
                    Issue("voucher_outside_coverage",group);
        }
        var counts=new CorrectionCounts(incoming.Count,existing.Count,matched.Count,changed.Count,sourceFailed?0:remainingNew.Count,
            !sourceFailed&&complete?remainingOld.Count:0,!sourceFailed&&!complete?remainingOld.Count:0,information,positions);
        // Hash bounded individual actions rather than building a second full
        // serialized copy of a large voucher population in memory.
        Charge(128L*(rows.Count+match.Identities.Length));
        var actionHashes=rows.Select(row=>
        {ct.ThrowIfCancellationRequested();return UploadIdentityValue.Hash(JsonSerializer.Serialize(row));}).ToArray();
        string key=UploadIdentityValue.Hash(JsonSerializer.Serialize(new {Policy="Corrections-v1-review",proof.CoverageKey,storeKey,profile,bindings,
            Identities=match.Identities,Actions=actionHashes,counts,Incomplete=incompleteGroups.Order(StringComparer.Ordinal)}));
        bool stale=request.ExpectedCorrectionKey is not null&&request.ExpectedCorrectionKey!=key||proof.Status=="confirmation_changed";
        bool passed=!sourceFailed&&proof.CoverageChecksPassed&&!stale&&issueCount==0;
        bool unchanged=passed&&counts.Changed==0&&counts.Added==0&&counts.Removed==0;
        bool noOp=unchanged&&information==0&&positions==0&&profile.AccountCode is null;
        string status=stale?"confirmation_changed":sourceFailed?"source_checks_failed":issueCount>0?"correction_checks_failed":noOp?"no_changes"
            :unchanged?"row_metadata_review_required":"changes_found";
        var page=new List<CorrectionRow>();long pageBytes=0;long offset=(long)(request.Page-1)*request.PageSize;
        foreach(var row in rows.Skip((int)Math.Min(offset,int.MaxValue)).Take(request.PageSize))
        {
            try {pageBytes+=UploadPreparationService.JsonSize(row,limits.MaximumResponseBytes/3-pageBytes);page.Add(row);}
            catch(UploadPreparationService.InvalidPreparation) { }
        }
        observe?.Invoke(rows);
        var output=new CorrectionOutput(status,proof,key,storeKey,counts,passed,unchanged,noOp,false,request.Page,request.PageSize,rows.Count,page,
            issueCount,issues,issueCount>issues.Count||page.Count<Math.Min(request.PageSize,Math.Max(0L,rows.Count-offset)),
            ["Read-only captured-snapshot review; signed plans, atomic current/history writes and live activation remain gated.",
             "Unchanged transaction rows do not certify ledger balance/coverage evidence. Missing immutable correction bindings require review."]);
        UploadPreparationService.JsonSize(output,limits.MaximumResponseBytes);return output;
    }
}
