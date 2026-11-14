using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Data;

namespace AuditPlatform.Api.TrialBalance;

internal static class TrialBalanceReviewWriter
{
    internal static TrialBalanceReviewPlan Plan(TrialBalanceReviewSnapshot snapshot,string kind,IReadOnlyList<TrialBalanceReviewChange> changes,CancellationToken ct)
    {
        var byId=snapshot.Rows.ToDictionary(r=>r.Id);var occupied=snapshot.Rows.ToDictionary(r=>r.Code,r=>r.Id,StringComparer.Ordinal);
        var edited=new Dictionary<int,TrialBalanceReviewChange>();var codes=new Dictionary<int,string>();
        foreach(var change in changes)
        {
            ct.ThrowIfCancellationRequested();
            if(!edited.TryAdd(change.AccountId,change))throw new TrialBalanceStorageRejected("invalid","tb_review_duplicate_account");
            if(!byId.TryGetValue(change.AccountId,out var row))throw new TrialBalanceStorageRejected("not_found","tb_review_account_not_found");
            if(row.Code!=change.ExpectedAccountCode)throw new TrialBalanceStorageRejected("conflict","tb_review_code_changed");
            if(kind=="codes")
            {
                string code=change.AccountCode??"";
                if(change.IsLeaf is not null||code.Length is <1 or >120||!Regex.IsMatch(code,@"^[0-9]+(?:-[0-9]+)*$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100)))throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_code");
                if(occupied.TryGetValue(code,out int id)&&id!=change.AccountId)throw new TrialBalanceStorageRejected("conflict","tb_review_occupied_code");
                if(code!=row.Code)codes[row.Id]=code;
            }
            else if(change.IsLeaf is null||change.AccountCode is not null)throw new TrialBalanceStorageRejected("invalid","tb_review_invalid_classification");
        }
        var final=snapshot.Rows.Select(r=>codes.GetValueOrDefault(r.Id,r.Code)).ToArray();
        if(final.Distinct(StringComparer.Ordinal).Count()!=final.Length)throw new TrialBalanceStorageRejected("conflict","tb_review_duplicate_code");
        var hierarchy=kind=="codes"&&codes.Count>0?TrialBalanceValidation.Hierarchy(final,ct):null;
        var planned=new List<TrialBalanceReviewPlanned>();int classifications=0,structural=0;
        foreach(var row in snapshot.Rows)
        {
            ct.ThrowIfCancellationRequested();string code=codes.GetValueOrDefault(row.Id,row.Code);string? parent=hierarchy is null?row.Parent:hierarchy[code].Parent;
            int level=hierarchy is null?row.Level:code.Split('-').Length;
            string classification=hierarchy is null||row.Manual?row.Classification:hierarchy[code].Classification;bool manual=row.Manual;
            if(kind=="classification"&&edited.TryGetValue(row.Id,out var edit)){classification=edit.IsLeaf==true?"leaf":"parent";manual=true;if(classification!=row.Classification||manual!=row.Manual)classifications++;}
            if(code!=row.Code||parent!=row.Parent||level!=row.Level||classification!=row.Classification||manual!=row.Manual)
            {planned.Add(new(row.Id,code,parent,level,classification,manual));if(parent!=row.Parent||level!=row.Level||classification!=row.Classification)structural++;}
        }
        TrialBalanceBudget.Size(planned,33554432);
        return new(planned,codes.Select(p=>(byId[p.Key].Code,p.Value)).ToArray(),codes.Count,classifications,structural);
    }
    internal static async Task Write(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,int upload,TrialBalanceReviewPlan plan,CancellationToken ct)
    {
        await TrialBalanceAssignmentStore.CarryCodesAsync(db,actor,client,plan.Pairs,true,ct);
        if(plan.Rows.Count==0)return;
        await using var update=db.CreateCommand("""
            UPDATE public.trial_balance_accounts a SET account_code=r.code,parent_account_code=r.parent,level=r.level,
              classification=r.classification,manually_overridden=r.manual
            FROM jsonb_to_recordset($1::jsonb) AS r(id integer,code text,parent text,level integer,classification text,manual boolean)
            WHERE a.id=r.id AND a.upload_id=$2
            """);update.Parameters.AddWithValue(JsonSerializer.Serialize(plan.Rows,TrialBalanceStorageJson.Options));update.Parameters.AddWithValue(upload);
        if(await update.ExecuteNonQueryAsync(ct)!=plan.Rows.Count)throw new TrialBalanceStorageRejected("conflict","tb_review_row_count_changed");
    }
    internal static void Verify(TrialBalanceReviewSnapshot before,TrialBalanceReviewSnapshot after,TrialBalanceReviewPlan plan)
    {
        if(before.ProtectedHash!=after.ProtectedHash||before.Rows.Count!=after.Rows.Count)throw new TrialBalanceStorageRejected("review","tb_review_source_changed");
        var expected=before.Rows.ToDictionary(r=>r.Id,r=>new TrialBalanceReviewPlanned(r.Id,r.Code,r.Parent,r.Level,r.Classification,r.Manual));
        foreach(var row in plan.Rows)expected[row.Id]=row;
        foreach(var row in after.Rows)if(!expected.TryGetValue(row.Id,out var match)||match!=new TrialBalanceReviewPlanned(row.Id,row.Code,row.Parent,row.Level,row.Classification,row.Manual))throw new TrialBalanceStorageRejected("review","tb_review_stored_result_mismatch");
    }
}
