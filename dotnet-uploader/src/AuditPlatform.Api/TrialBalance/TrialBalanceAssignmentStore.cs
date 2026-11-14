using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.TrialBalance;

internal static class TrialBalanceAssignmentStore
{
    internal sealed record MappingTable(string Name,string[] Columns);
    internal static readonly MappingTable[] Tables=[
        new("account_category_assignments",["category_id"]),
        new("account_control_assignments",["verification_workstream","financial_statement_category_id","note_category_id"]),
        new("account_control_cashflow_assignments",["section","category_label","cash_bank_role","treatment_type"]),
        new("account_control_cashflow_category_assignments",["category_id"])];

    private static string OriginalCode(WorkbookCell cell,string original)
    {
        if(cell.Kind!=WorkbookCellKind.Number) return original;
        // Reproduce the established safe pandas numeric-code interpretation.
        // Ambiguous old numeric identities need an explicit later adoption review.
        var value=UploadExactAmount.Parse(cell.NumberToken!);
        if(value.HasLegacyConversionRisk(cell)||!double.TryParse(cell.NumberToken,NumberStyles.Float,CultureInfo.InvariantCulture,out double number)
            ||!double.IsFinite(number)||Math.Abs(number)>9007199254740991)
            throw new TrialBalanceStorageRejected("review","tb_source_code_compatibility_review");
        return number==0?"0":number==Math.Truncate(number)?number.ToString("0",CultureInfo.InvariantCulture):number.ToString("R",CultureInfo.InvariantCulture).Replace('E','e');
    }
    internal static Dictionary<int,string> Originals(TrialBalanceReview review,TrialBalanceInput input)
    {
        var result=new Dictionary<int,string>();
        int position=review.Headers.Single(h=>h.Label==input.Mapping["account_code_column"]).Position;
        var rows=review.SourceRows.ToDictionary(r=>r.ExcelRow);
        foreach(var account in review.Accounts)
        {
            string original=OriginalCode(TrialBalanceParser.Cell(rows[account.ExcelRow],position),account.SourceAccountCode);result[account.ExcelRow]=original;
            if(input.CodeOverrides?.ContainsKey(account.ExcelRow)!=true&&original!=account.AccountCode)
                throw new TrialBalanceStorageRejected("review","tb_code_interpretation_review");
        }
        return result;
    }
    internal static async Task CarryAsync(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,
        TrialBalanceReview review,Dictionary<int,string> originals,CancellationToken ct)
    {
        var pairs=review.Accounts.Select(a=>(Source:originals[a.ExcelRow],Target:a.AccountCode)).Where(p=>p.Source!=p.Target).Distinct().ToArray();
        await CarryCodesAsync(db,actor,client,pairs,true,ct);
    }
    internal static async Task CarryCodesAsync(PostgresTransactionContext db,TrialBalanceStorageActor actor,int client,
        IReadOnlyList<(string Source,string Target)> pairs,bool apply,CancellationToken ct)
    {
        if(pairs.Count==0) return;
        var codes=pairs.SelectMany(p=>new[]{p.Source,p.Target}).Distinct().ToArray();
        foreach(var table in Tables)
        {
            // Four bounded reads/writes, rather than a network trip per account.
            var existing=new Dictionary<string,JsonElement>(StringComparer.Ordinal);long bytes=0;
            await using(var read=db.CreateCommand($"SELECT account_code,left(to_jsonb(t)::text,1048577) FROM public.{TrialBalanceStorageStore.Quote(table.Name)} t WHERE client_id=$1 AND account_code=ANY($2) ORDER BY id"))
            {
                read.Parameters.AddWithValue(client);read.Parameters.AddWithValue(codes);await using var reader=await read.ExecuteReaderAsync(ct);
                while(await reader.ReadAsync(ct))
                {string raw=reader.GetString(1);bytes+=System.Text.Encoding.UTF8.GetByteCount(raw);if(raw.Length>1048576||bytes>33554432||existing.Count>=100000) throw new TrialBalanceStorageRejected("too_large","tb_assignment_capacity");using var json=JsonDocument.Parse(raw);if(!existing.TryAdd(reader.GetString(0),json.RootElement.Clone())) throw new TrialBalanceStorageRejected("review","tb_duplicate_assignments");}
            }
            var planned=new Dictionary<string,JsonElement>(StringComparer.Ordinal);
            foreach(var pair in pairs)
            {
                ct.ThrowIfCancellationRequested();if(!existing.TryGetValue(pair.Source,out var source)||!Assigned(table,source)) continue;
                if(existing.TryGetValue(pair.Target,out var target))
                {if(table.Columns.All(c=>JsonElement.DeepEquals(source.GetProperty(c),target.GetProperty(c)))) continue;if(Assigned(table,target)) throw new TrialBalanceStorageRejected("conflict","tb_assignment_conflict");}
                planned[pair.Target]=source;existing[pair.Target]=source;
            }
            if(planned.Count==0||!apply) continue;
            var payload=planned.Select(p=>new {account_code=p.Key,values=table.Columns.ToDictionary(c=>c,c=>p.Value.GetProperty(c))}).ToArray();
            TrialBalanceBudget.Size(payload,33554432);string jsonText=JsonSerializer.Serialize(payload);
            string columns=string.Join(',',table.Columns.Select(TrialBalanceStorageStore.Quote));
            string values=string.Join(',',table.Columns.Select(c=>"(jsonb_populate_record(NULL::public."+TrialBalanceStorageStore.Quote(table.Name)+",e->'values'))."+TrialBalanceStorageStore.Quote(c)));
            string metadata=table.Name=="account_category_assignments"?"":",updated_by";
            string sql=$"INSERT INTO public.{TrialBalanceStorageStore.Quote(table.Name)}(client_id,account_code,{columns}{metadata}) SELECT $1,e->>'account_code',{values}{(metadata==""?"":",$3")} FROM jsonb_array_elements($2::jsonb) e "+
                "ON CONFLICT(client_id,account_code) DO UPDATE SET "+string.Join(',',table.Columns.Select(c=>TrialBalanceStorageStore.Quote(c)+"=EXCLUDED."+TrialBalanceStorageStore.Quote(c)))+",updated_at=now()"+(metadata==""?"":",updated_by=EXCLUDED.updated_by");
            await using var write=db.CreateCommand(sql);write.Parameters.AddWithValue(client);write.Parameters.AddWithValue(jsonText);if(metadata!="") write.Parameters.AddWithValue(actor.Name);
            if(await write.ExecuteNonQueryAsync(ct)!=planned.Count) throw new TrialBalanceStorageRejected("conflict","tb_assignment_changed");
        }
    }
    private static bool Assigned(MappingTable table,JsonElement row)
    {
        string? Text(string key)=>row.GetProperty(key).ValueKind==JsonValueKind.Null?null:row.GetProperty(key).ToString();
        if(table.Name=="account_control_cashflow_assignments") return Text("section") is not(null or "unassigned")||!string.IsNullOrEmpty(Text("category_label"))
            ||Text("cash_bank_role") is not(null or "none")||Text("treatment_type") is not(null or "standard");
        return table.Columns.Any(c=>row.GetProperty(c).ValueKind!=JsonValueKind.Null);
    }
}
