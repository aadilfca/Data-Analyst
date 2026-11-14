using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Workbooks;
using Amount=AuditPlatform.Api.Uploads.UploadExactAmount;

namespace AuditPlatform.Api.TrialBalance;

internal static class TrialBalanceValidation
{
    private sealed record Account(TrialBalanceSourceRow Source,string Original,string Code,string? Name,Amount[] Values)
    {public string Classification="leaf",Automatic="leaf";public string? Parent;}
    private static readonly Amount Tolerance=Amount.Parse("0.01");
    private static string Label(string role)=>role.Replace("_column","",StringComparison.Ordinal).Replace('_',' ');
    private static IReadOnlyDictionary<string,string> Tokens(Amount[] amounts)=>new ReadOnlyDictionary<string,string>(
        TrialBalanceMapping.AmountRoles.Select((role,i)=>KeyValuePair.Create(role,amounts[i].ToFixedString())).ToDictionary());
    private static bool Outside(Amount value)=>value.Abs().CompareTo(Tolerance)>0;
    private static bool ConversionRisk(Amount value)=>value.HasLegacyConversionRisk(new(0,0,WorkbookCellKind.Text,Text:value.ToFixedString()));
    internal static TrialBalanceReview Build(string sheet,string hash,string fingerprint,IReadOnlyList<TrialBalanceHeader> headers,
        IReadOnlyList<TrialBalanceSourceRow> rows,TrialBalanceInput input,Dictionary<string,int> binding,TrialBalanceLimits limits,CancellationToken ct)
    {
        var errors=new List<TrialBalanceIssue>();var warnings=new List<TrialBalanceIssue>();
        void Issue(int? row,string field,string code,string message,bool warning=false)
        {
            ct.ThrowIfCancellationRequested();
            if(errors.Count+warnings.Count>=limits.MaximumIssues) throw new TrialBalanceRejected("issue_capacity","Too many Trial Balance issues; review a smaller source.");
            (warning?warnings:errors).Add(new(row,field,code,message,warning?"warning":"error"));
        }
        bool HasValue(WorkbookCell c)=>!string.IsNullOrWhiteSpace(TrialBalanceParser.Text(c))||c.FormulaState==WorkbookFormulaState.MissingResult;
        int last=rows.Where(r=>r.Cells.Any(HasValue)).Select(r=>r.ExcelRow).DefaultIfEmpty(1).Max();
        var rowNumbers=rows.Select(r=>r.ExcelRow).ToHashSet();
        bool InRange(int number)=>number>=2&&number<=last&&rowNumbers.Contains(number);
        foreach(var (number,value) in input.CodeOverrides!)
            if(!InRange(number)||string.IsNullOrWhiteSpace(value)||value.Trim().Length>120)
                throw new TrialBalanceRejected("invalid_correction","Code corrections require an actual source row and 1–120 characters.");
        foreach(var (number,value) in input.ClassificationOverrides!)
            if(!InRange(number)||value is not("leaf" or "parent")) throw new TrialBalanceRejected("invalid_correction","Classification corrections require an actual source row and leaf/parent.");
        var excluded=input.ExcludedExcelRows!.ToHashSet();
        if(excluded.Count!=input.ExcludedExcelRows!.Count||excluded.Any(n=>!InRange(n))) throw new TrialBalanceRejected("invalid_exclusion","Subtotal exclusions must be distinct actual source row numbers.");
        if(input.NoTotalRow==(input.TotalExcelRow is not null)) throw new TrialBalanceRejected("total_choice_required","Choose one actual ERP total row or explicitly choose No total row.");
        Amount[]? control=null;
        Amount[] Amounts(TrialBalanceSourceRow source,bool total=false)
        {
            var values=new Amount[6];int i=0;
            foreach(string role in TrialBalanceMapping.AmountRoles)
            {
                var cell=TrialBalanceParser.Cell(source,binding[role]);string text=TrialBalanceParser.Text(cell).Trim();
                try
                {
                    if(cell.FormulaState==WorkbookFormulaState.MissingResult||cell.Kind is not(WorkbookCellKind.Blank or WorkbookCellKind.Text or WorkbookCellKind.Number)) throw new FormatException();
                    if(text.Length==0) {values[i++]=Amount.Zero;continue;}
                    bool negative=text.StartsWith('(')&&text.EndsWith(')');
                    if(negative) text=text[1..^1];text=text.Replace(",","",StringComparison.Ordinal).Trim();
                    var value=Amount.Parse(text);if(negative) value=Amount.Zero-value;values[i]=value;
                    var normalized=new WorkbookCell(source.ExcelRow,cell.Column,WorkbookCellKind.Text,Text:value.ToFixedString());
                    if(value.HasLegacyConversionRisk(normalized)) Issue(source.ExcelRow,Label(role),"precision_review_required","This amount needs legacy-conversion precision review.");
                }
                catch(FormatException)
                {
                    if(total) throw new TrialBalanceRejected("invalid_total_row","The ERP total row contains an invalid mapped amount.");
                    values[i]=Amount.Zero;Issue(source.ExcelRow,Label(role),"invalid_amount","Enter a finite saved number or blank.");
                }
                i++;
            }
            return values;
        }
        if(input.TotalExcelRow is {} totalRow)
        {
            if(!InRange(totalRow)||input.CodeOverrides.ContainsKey(totalRow)||input.ClassificationOverrides.ContainsKey(totalRow))
                throw new TrialBalanceRejected("invalid_total_row","The ERP total must be an actual row without account corrections.");
            var total=rows.Single(r=>r.ExcelRow==totalRow);
            if(!TrialBalanceMapping.AmountRoles.Any(role=>HasValue(TrialBalanceParser.Cell(total,binding[role])))) throw new TrialBalanceRejected("invalid_total_row","The ERP total row contains no mapped amounts.");
            control=Amounts(total,true);excluded.Remove(totalRow);
        }
        var accounts=new List<Account>();var seen=new Dictionary<string,int>(StringComparer.Ordinal);
        foreach(var row in rows)
        {
            ct.ThrowIfCancellationRequested();if(row.ExcelRow==input.TotalExcelRow) continue;
            var codeCell=TrialBalanceParser.Cell(row,binding["account_code_column"]);
            string original=TrialBalanceParser.Text(codeCell).Trim(),code=input.CodeOverrides.GetValueOrDefault(row.ExcelRow,original).Trim();
            bool corrected=input.CodeOverrides.ContainsKey(row.ExcelRow);
            if(!corrected&&codeCell.Kind==WorkbookCellKind.Number)
            {
                // pandas canonicalizes safe integral numeric codes (1.0, 1E3).
                // Keep the original lexeme in source evidence; never guess a
                // large or differently formatted numeric identity.
                bool conversionRisk=Amount.Parse(original).HasLegacyConversionRisk(codeCell);
                if(!conversionRisk&&double.TryParse(original,NumberStyles.Float,CultureInfo.InvariantCulture,out double number)&&double.IsFinite(number)
                    &&Math.Abs(number)<=9007199254740991&&number==Math.Truncate(number)) code=number.ToString("0",CultureInfo.InvariantCulture);
                else if(conversionRisk||!double.TryParse(original,NumberStyles.Float,CultureInfo.InvariantCulture,out number)||!double.IsFinite(number)||Math.Abs(number)>9007199254740991||original!=number.ToString("R",CultureInfo.InvariantCulture))
                    Issue(row.ExcelRow,"Account Code","numeric_code_review_required","Confirm a text correction for this numeric account code.");
            }
            bool present=binding.Values.Any(pos=>HasValue(TrialBalanceParser.Cell(row,pos)));
            if(!present) {if(excluded.Contains(row.ExcelRow)) throw new TrialBalanceRejected("invalid_exclusion","An empty row cannot be excluded as a subtotal.");continue;}
            if(excluded.Contains(row.ExcelRow))
            {
                if(original.Length>0||input.CodeOverrides.ContainsKey(row.ExcelRow)||input.ClassificationOverrides.ContainsKey(row.ExcelRow))
                    throw new TrialBalanceRejected("invalid_exclusion","Only source subtotal rows without account codes or corrections may be excluded.");
                continue;
            }
            if(code.Length==0) {Issue(row.ExcelRow,"Account Code","blank_code","Account code is blank.");continue;}
            if(code.Length>120) Issue(row.ExcelRow,"Account Code","code_length","Account code cannot exceed 120 characters.");
            if(!corrected&&(codeCell.Kind==WorkbookCellKind.DateTime||LooksLikeDate(original))) Issue(row.ExcelRow,"Account Code","date_code","This code appears to have been converted to an Excel date; confirm a correction.");
            if(!corrected&&(codeCell.Kind==WorkbookCellKind.Error||codeCell.FormulaState==WorkbookFormulaState.MissingResult))
                Issue(row.ExcelRow,"Account Code","unavailable_code","Recalculate and save the source account code.");
            if(seen.TryGetValue(code,out int prior)) Issue(row.ExcelRow,"Account Code","duplicate_code",$"Account code {code} duplicates Excel row {prior}.");else seen[code]=row.ExcelRow;
            var name=TrialBalanceParser.Cell(row,binding["account_name_column"]);
            if(name.Kind==WorkbookCellKind.Error||name.FormulaState==WorkbookFormulaState.MissingResult) Issue(row.ExcelRow,"Account Name","unavailable_name","Recalculate and save the source account name.");
            string title=TrialBalanceParser.Text(name).Trim();accounts.Add(new(row,original,code,title.Length==0?null:title,Amounts(row)));
        }
        if(accounts.Count==0) Issue(null,"Rows","no_accounts","No Trial Balance account rows were found.");
        var hierarchy=Hierarchy(accounts.Select(a=>a.Code),ct);
        foreach(var account in accounts)
        {
            ct.ThrowIfCancellationRequested();var h=hierarchy[account.Code];account.Automatic=h.Classification;account.Parent=h.Parent;
            account.Classification=input.ClassificationOverrides.GetValueOrDefault(account.Source.ExcelRow,h.Classification);
        }
        var parents=accounts.Where(a=>a.Classification=="parent").Select(a=>a.Source.ExcelRow).ToHashSet();
        foreach(var issue in errors.Where(e=>e.ExcelRow is {} r&&parents.Contains(r)&&e.Field=="Account Code"&&e.Code is "code_length" or "date_code").ToArray())
        {errors.Remove(issue);warnings.Add(issue with {Severity="warning"});}
        foreach(var account in accounts.Where(a=>a.Classification=="parent"&&a.Automatic!="parent"))
            Issue(account.Source.ExcelRow,"Account Code","unlinked_parent","Parent has no linked child accounts; confirm intent before import.",true);
        var leaves=accounts.Where(a=>a.Classification=="leaf").ToArray();
        if(leaves.Length==0) Issue(null,"Rows","no_leaves","At least one Leaf account is required.");
        var checks=new List<TrialBalanceCheck>{new("account_codes",errors.Count==0?"pass":"fail")};
        var totals=new Amount[6];foreach(var leaf in leaves) {ct.ThrowIfCancellationRequested();for(int i=0;i<6;i++) totals[i]+=leaf.Values[i];}
        if(errors.Count==0&&totals.Any(ConversionRisk)) Issue(null,"Footings","precision_review_required","Exact aggregate totals need legacy-conversion precision review.");
        var comparisons=new List<TrialBalanceControl>();string sourceStatus=control is null?"not_applicable":"blocked";
        if(control is not null&&errors.Count==0)
        {
            for(int i=0;i<6;i++) comparisons.Add(new(Label(TrialBalanceMapping.AmountRoles[i]),control[i].ToFixedString(),totals[i].ToFixedString(),(totals[i]-control[i]).ToFixedString(),!Outside(totals[i]-control[i])));
            sourceStatus=comparisons.All(c=>c.WithinTolerance)?"pass":"fail";
            if(sourceStatus=="fail") Issue(null,"ERP total","erp_total_mismatch","Original leaf totals differ from the selected ERP total.");
        }
        checks.Add(new("source_total",sourceStatus));var adjusted=totals.ToArray();var roll=new List<TrialBalanceDifference>();var global=new List<TrialBalanceDifference>();
        if(errors.Count==0)
        {
            foreach(var leaf in leaves)
            {
                ct.ThrowIfCancellationRequested();var expected=leaf.Values[0]-leaf.Values[1]+leaf.Values[2]-leaf.Values[3];var closing=leaf.Values[4]-leaf.Values[5];var difference=expected-closing;
                if(ConversionRisk(expected)||ConversionRisk(closing)||ConversionRisk(difference))
                {Issue(leaf.Source.ExcelRow,"Roll-forward","precision_review_required","Exact roll-forward controls need legacy-conversion precision review.");continue;}
                if(!Outside(difference)) continue;
                var delta=new Amount[6];delta[difference.CompareTo(Amount.Zero)>0?4:5]=difference.Abs();adjusted[4]+=delta[4];adjusted[5]+=delta[5];
                roll.Add(new(leaf.Source.ExcelRow,leaf.Code,leaf.Name,Tokens(delta),ExpectedClosingNet:expected.ToFixedString(),OriginalClosingNet:closing.ToFixedString(),DifferenceNet:difference.ToFixedString()));
                Issue(leaf.Source.ExcelRow,"Roll-forward","rollforward_difference","Opening plus movement differs from closing within the 0.01 control.",true);
            }
        }
        checks.Add(new("roll_forward",errors.Count>0?"blocked":roll.Count>0?"adjusted":"pass",roll.Count));
        if(errors.Count==0)
        {
            for(int i=0;i<6;i+=2)
            {
                var net=adjusted[i]-adjusted[i+1];if(!Outside(net)) continue;int side=net.CompareTo(Amount.Zero)>0?1:0;var delta=new Amount[6];delta[i+side]=net.Abs();if(i!=4) delta[4+side]=net.Abs();
                global.Add(new(null,null,new[]{"Opening footing","Period movement footing","Closing rounding footing"}[i/2],Tokens(delta),true));
                for(int j=0;j<6;j++) adjusted[j]+=delta[j];
            }
            if(global.Count>0) Issue(null,"Footings","global_difference","Vertical differences are separate Difference lines; source amounts stay unchanged.",true);
        }
        var pairs=new Dictionary<string,TrialBalancePair>();
        for(int i=0;i<6;i+=2)
        {
            string period=new[]{"opening","period","closing"}[i/2];pairs[period]=new((adjusted[i]-adjusted[i+1]).ToFixedString(),!Outside(adjusted[i]-adjusted[i+1]));
            bool changed=global.Any(g=>Amount.Parse(g.Amounts[TrialBalanceMapping.AmountRoles[i]]).IsZero==false||Amount.Parse(g.Amounts[TrialBalanceMapping.AmountRoles[i+1]]).IsZero==false);
            checks.Add(new(period+"_balance",errors.Count>0?"blocked":changed?"adjusted":"pass"));
        }
        var adjustments=Enumerable.Range(0,6).Select(i=>adjusted[i]-totals[i]).ToArray();
        if(errors.Count==0&&(adjusted.Any(ConversionRisk)||adjustments.Any(ConversionRisk)))
            Issue(null,"Footings","precision_review_required","Exact draft totals need legacy-conversion precision review.");
        var draft=errors.Count==0?new TrialBalanceReconciliation(Tokens(totals),Tokens(adjustments),Tokens(adjusted),new ReadOnlyDictionary<string,TrialBalancePair>(pairs)):null;
        bool? erp=control is null?null:Enumerable.Range(0,3).Any(i=>Outside(control[i*2]-control[i*2+1]));
        return new(sheet,hash,fingerprint,headers,TrialBalanceMapping.Suggest(headers),rows,accounts.Select(a=>new TrialBalanceAccount(a.Source.ExcelRow,a.Original,a.Code,a.Name,a.Classification,a.Automatic,a.Parent,Tokens(a.Values))).ToArray(),
            errors.AsReadOnly(),warnings.AsReadOnly(),checks.AsReadOnly(),comparisons.AsReadOnly(),Tokens(totals),roll.AsReadOnly(),global.AsReadOnly(),draft,excluded.Order().ToArray(),input.TotalExcelRow,input.NoTotalRow,erp);
    }
    private static bool LooksLikeDate(string value)
    {
        var match=Regex.Match(value,@"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?: 00:00:00)?$",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));
        return match.Success&&DateTime.TryParseExact(value.Split(' ')[0],["yyyy-M-d","yyyy-MM-dd","yyyy-M-dd","yyyy-MM-d"],CultureInfo.InvariantCulture,DateTimeStyles.None,out _);
    }
    internal static Dictionary<string,(string Classification,string? Parent)> Hierarchy(IEnumerable<string> codes,CancellationToken ct)
    {
        static string Normal(string p)=>p.Length>0&&p.All(c=>c is >= '0' and <= '9')?(p.TrimStart('0') is {Length:>0} n?n:"0"):p;
        var set=codes.ToHashSet(StringComparer.Ordinal);var rawRoot=new Node();var normalRoot=new Node();
        foreach(string code in set)
        {
            ct.ThrowIfCancellationRequested();var raw=rawRoot;var normal=normalRoot;
            foreach(string part in code.Split('-')) {ct.ThrowIfCancellationRequested();raw=raw.Child(part);normal=normal.Child(Normal(part));}
            raw.Codes.Add(code);normal.Codes.Add(code);
        }
        var result=new Dictionary<string,(string Classification,string? Parent)>(StringComparer.Ordinal);
        foreach(string code in set)
        {
            ct.ThrowIfCancellationRequested();string? parent=null;var parts=code.Split('-');Node? raw=rawRoot,normal=normalRoot;
            for(int i=0;i<parts.Length-1;i++)
            {
                ct.ThrowIfCancellationRequested();raw=raw?.Children.GetValueOrDefault(parts[i]);normal=normal?.Children.GetValueOrDefault(Normal(parts[i]));
                string? candidate=raw?.Codes.SingleOrDefault()??(normal?.Codes.Count==1?normal.Codes[0]:null);
                if(candidate is not null) parent=candidate;
            }
            result[code]=("leaf",parent);
        }
        foreach(string parent in result.Values.Select(v=>v.Parent).OfType<string>().Distinct().ToArray())
        {ct.ThrowIfCancellationRequested();result[parent]=("parent",result[parent].Parent);}
        return result;
    }
    private sealed class Node
    {
        internal Dictionary<string,Node> Children {get;}=new(StringComparer.Ordinal);
        internal List<string> Codes {get;}=[];
        internal Node Child(string segment)
        {if(!Children.TryGetValue(segment,out var value)) Children.Add(segment,value=new());return value;}
    }
}
