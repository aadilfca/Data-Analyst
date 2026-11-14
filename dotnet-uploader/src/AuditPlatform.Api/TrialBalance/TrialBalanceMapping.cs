using System.Text.RegularExpressions;

namespace AuditPlatform.Api.TrialBalance;

public static class TrialBalanceMapping
{
    public static IReadOnlyList<string> AmountRoles {get;}=Array.AsReadOnly(new[]{"opening_dr_column","opening_cr_column","period_dr_column","period_cr_column","closing_dr_column","closing_cr_column"});
    public static IReadOnlyList<string> RequiredRoles {get;}=Array.AsReadOnly(new[]{"account_code_column","account_name_column"}.Concat(AmountRoles).ToArray());
    private static readonly (string Role,string[] Names)[] Roles=[
        ("account_code_column",["code","accountcode","ledgercode","accode","glcode","accountno","acno"]),
        ("account_name_column",["name","accountname","particulars","description","accounttitle","title"]),
        ("type_column",["type","mastersub","accounttype","classification"]),
        ("opening_dr_column",["openingdr","openingdebit","openingbalancedr","openingbaldr","obdr","openingdramount"]),
        ("opening_cr_column",["openingcr","openingcredit","openingbalancecr","openingbalcr","obcr","openingcramount"]),
        ("period_dr_column",["perioddr","perioddebit","debit","dr","debitamount","duringperioddebit","movementdr","transactiondebit"]),
        ("period_cr_column",["periodcr","periodcredit","credit","cr","creditamount","duringperiodcredit","movementcr","transactioncredit"]),
        ("closing_dr_column",["closingdr","closingdebit","closingbalancedr","closingbaldr","cbdr","closingdramount"]),
        ("closing_cr_column",["closingcr","closingcredit","closingbalancecr","closingbalcr","cbcr","closingcramount"])];
    private static string Normal(string text)=>Regex.Replace(text.ToLowerInvariant(),"[^a-z0-9]","");
    public static IReadOnlyDictionary<string,string?> Suggest(IReadOnlyList<TrialBalanceHeader> headers)
    {
        var used=new HashSet<int>();var result=new Dictionary<string,string?>();
        foreach(var (role,names) in Roles)
        {
            var available=headers.Where(h=>!h.Ambiguous&&!used.Contains(h.Position)&&Normal(h.Label).Length>0).ToArray();
            var matches=available.Where(h=>names.Contains(Normal(h.Label))).ToArray();
            if(matches.Length==0) matches=available.Where(h=>names.Any(n=>n.Contains(Normal(h.Label),StringComparison.Ordinal)||Normal(h.Label).Contains(n,StringComparison.Ordinal))).ToArray();
            // Suggestions are advisory. Multiple plausible headers need a choice.
            var selected=matches.Length==1?matches[0]:null;result[role]=selected?.Label;
            if(selected is not null) used.Add(selected.Position);
        }
        return result;
    }
    internal static Dictionary<string,int> Bind(IReadOnlyDictionary<string,string?> mapping,IReadOnlyList<TrialBalanceHeader> headers)
    {
        if(mapping.Keys.Any(k=>!Roles.Any(r=>r.Role==k))) throw new TrialBalanceRejected("invalid_mapping","Unknown Trial Balance mapping role.");
        var result=new Dictionary<string,int>();
        foreach(var (role,_) in Roles)
        {
            string? label=mapping.GetValueOrDefault(role);
            if(string.IsNullOrWhiteSpace(label))
            {if(RequiredRoles.Contains(role)) throw new TrialBalanceRejected("invalid_mapping","All eight required Trial Balance fields must be mapped.");continue;}
            var matches=headers.Where(h=>h.Label==label).ToArray();
            if(matches.Length!=1||matches[0].Ambiguous) throw new TrialBalanceRejected("ambiguous_mapping","Map an unambiguous actual source column for each field.");
            result[role]=matches[0].Position;
        }
        if(RequiredRoles.Select(r=>result[r]).Distinct().Count()!=RequiredRoles.Count)
            throw new TrialBalanceRejected("invalid_mapping","Required Trial Balance fields must use distinct source columns.");
        return result;
    }
}
