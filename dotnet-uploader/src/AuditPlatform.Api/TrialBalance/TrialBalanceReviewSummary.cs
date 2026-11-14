using System.Text.Json;
using Amount=AuditPlatform.Api.Uploads.UploadExactAmount;

namespace AuditPlatform.Api.TrialBalance;

internal static class TrialBalanceReviewSummary
{
    internal static Dictionary<string,(string Classification,string? Parent)> Hierarchy(TrialBalanceReviewSnapshot source,CancellationToken ct)
        =>TrialBalanceValidation.Hierarchy(source.Rows.Select(r=>r.Code),ct);
    internal static bool Leaf(TrialBalanceReviewRow row,Dictionary<string,(string Classification,string? Parent)> hierarchy)
        =>(row.Manual?row.Classification:hierarchy[row.Code].Classification)=="leaf";
    internal static Dictionary<string,object?> Controls(TrialBalanceReviewSnapshot source,Dictionary<string,(string Classification,string? Parent)> hierarchy,CancellationToken ct)
    {
        // Historical classifications are server-side evidence, never a large
        // browser payload. Read freshness never rewrites the imported controls.
        string[] keys=["checks","source_total_comparison","total_excel_row","no_total_row","erp_unbalanced","global_adjustments_automatic"];
        var result=new Dictionary<string,object?>();foreach(string key in keys)if(source.Controls.TryGetProperty(key,out var value))result[key]=value;
        var current=source.Rows.OrderBy(r=>r.Code,StringComparer.Ordinal).Select(r=>new[]{r.Code,Leaf(r,hierarchy)?"leaf":"parent"}).ToArray();
        bool stale=false;
        if(source.Controls.TryGetProperty("account_classifications",out var historic)&&historic.ValueKind==JsonValueKind.Array&&historic.GetArrayLength()>0)
            stale=!JsonElement.DeepEquals(historic,JsonSerializer.SerializeToElement(current));
        result["stale"]=stale;
        try
        {
            var original=TrialBalanceMapping.AmountRoles.ToDictionary(r=>r,_=>Amount.Zero);var adjusted=new Dictionary<string,Amount>(original);
            var leaves=source.Rows.Where(r=>Leaf(r,hierarchy)).ToArray();var ids=leaves.Select(r=>r.Id).ToHashSet();
            foreach(var row in leaves) {ct.ThrowIfCancellationRequested();foreach(string role in TrialBalanceMapping.AmountRoles)original[role]+=Parse(row.Values.GetProperty(role.Replace("_column","",StringComparison.Ordinal)));}
            foreach(string role in TrialBalanceMapping.AmountRoles)adjusted[role]=original[role];
            foreach(var entry in source.Adjustments)if(ids.Contains(entry.GetProperty("account_id").GetInt32()))
                foreach(string side in new[]{"dr","cr"})adjusted["closing_"+side+"_column"]+=Parse(entry.GetProperty("closing_"+side+"_adjustment"));
            foreach(var entry in Globals(source))foreach(string role in TrialBalanceMapping.AmountRoles)
                if(entry.TryGetProperty(role.Replace("_column","_adjustment",StringComparison.Ordinal),out var value))adjusted[role]+=Parse(value);
            var outputs=original.Values.Concat(adjusted.Values).Concat(original.Select(p=>adjusted[p.Key]-p.Value))
                .Concat(new[]{"opening","period","closing"}.Select(p=>adjusted[p+"_dr_column"]-adjusted[p+"_cr_column"]));
            foreach(var value in outputs)if(value.HasLegacyConversionRisk(new(0,0,AuditPlatform.Api.Workbooks.WorkbookCellKind.Text,Text:value.ToFixedString())))throw new FormatException();
            var reconciliation=new {original_totals=original.ToDictionary(p=>p.Key,p=>p.Value.ToFixedString()),
                adjustment_totals=original.ToDictionary(p=>p.Key,p=>(adjusted[p.Key]-p.Value).ToFixedString()),adjusted_totals=adjusted.ToDictionary(p=>p.Key,p=>p.Value.ToFixedString()),
                pair_differences=new[]{"opening","period","closing"}.ToDictionary(p=>p,p=>new {difference=(adjusted[p+"_dr_column"]-adjusted[p+"_cr_column"]).ToFixedString(),within_tolerance=(adjusted[p+"_dr_column"]-adjusted[p+"_cr_column"]).Abs().CompareTo(Amount.Parse("0.01"))<=0})};
            result["current_reconciliation"]=reconciliation;result["current_reconciliation_error"]=null;result["draft_reconciliation"]=stale?null:reconciliation;
        }
        catch(Exception e) when(e is FormatException or InvalidOperationException or KeyNotFoundException)
        {result["current_reconciliation"]=null;result["draft_reconciliation"]=null;result["current_reconciliation_error"]="Current saved footing is unavailable because a saved amount or Difference entry is invalid or cannot be displayed safely.";}
        return result;
    }
    internal static Amount Parse(JsonElement value)
    {
        if(value.ValueKind==JsonValueKind.Null)return Amount.Zero;
        if(value.ValueKind is not(JsonValueKind.String or JsonValueKind.Number))throw new FormatException();
        string raw=value.ValueKind==JsonValueKind.String?value.GetString()!:value.GetRawText();raw=raw.Trim();if(raw.Length==0)return Amount.Zero;
        bool negative=raw.StartsWith('(')&&raw.EndsWith(')');if(negative)raw=raw[1..^1];var result=Amount.Parse(raw.Replace(",","",StringComparison.Ordinal));return negative?Amount.Zero-result:result;
    }
    internal static IReadOnlyList<JsonElement> Globals(TrialBalanceReviewSnapshot source)
    {
        if(!source.Controls.TryGetProperty("global_adjustments",out var rows))return [];
        if(rows.ValueKind!=JsonValueKind.Array)throw new FormatException();return rows.EnumerateArray().ToArray();
    }
}
