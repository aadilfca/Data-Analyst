using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.TrialBalance;
using AuditPlatform.Api.Uploads;
using AuditPlatform.Api.Workbooks;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class TrialBalanceGoldenFixture : IAsyncLifetime
{
    public string DirectoryPath {get;private set;}="";
    public JsonDocument Oracle {get;private set;}=null!;
    public static string Root()
    {for(var d=new DirectoryInfo(AppContext.BaseDirectory);d is not null;d=d.Parent) if(File.Exists(Path.Combine(d.FullName,"AuditPlatform.slnx"))) return d.FullName;throw new InvalidOperationException("Solution not found");}
    public async ValueTask InitializeAsync()
    {
        string root=Root();DirectoryPath=Path.Combine(root,".tools","p34-"+Guid.NewGuid().ToString("N"));
        var start=new ProcessStartInfo(Path.Combine(root,"..","venv","Scripts","python.exe")) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        start.ArgumentList.Add(Path.Combine(root,"tests","p34_trial_balance_oracle.py"));start.ArgumentList.Add(DirectoryPath);
        using var process=Process.Start(start)!;var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();
        using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(90));
        try {await process.WaitForExitAsync(deadline.Token);}catch{process.Kill(entireProcessTree:true);throw;}
        Assert.True(process.ExitCode==0,await output+await errors);
        Oracle=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(DirectoryPath,"oracle.json")));
    }
    public ValueTask DisposeAsync() {Oracle?.Dispose();return ValueTask.CompletedTask;}
}

public sealed class TrialBalanceParsingTests(TrialBalanceGoldenFixture fixture) : IClassFixture<TrialBalanceGoldenFixture>
{
    private static CancellationToken Ct=>TestContext.Current.CancellationToken;
    private string PathOf(JsonElement c)=>Path.Combine(fixture.DirectoryPath,c.GetProperty("file").GetString()!);
    private static Dictionary<int,string>? Overrides(JsonElement value)=>value.ValueKind==JsonValueKind.Null?null:
        value.EnumerateObject().ToDictionary(p=>int.Parse(p.Name,CultureInfo.InvariantCulture),p=>p.Value.GetString()!);
    private static TrialBalanceInput Input(JsonElement c)
    {
        var r=c.GetProperty("request");return new(r.GetProperty("mapping").EnumerateObject().ToDictionary(p=>p.Name,p=>p.Value.GetString()),
            Overrides(r.GetProperty("code_overrides")),Overrides(r.GetProperty("classification_overrides")),
            r.GetProperty("excluded_excel_rows").ValueKind==JsonValueKind.Null?null:r.GetProperty("excluded_excel_rows").EnumerateArray().Select(e=>e.GetInt32()).ToArray(),
            r.GetProperty("total_excel_row").ValueKind==JsonValueKind.Null?null:r.GetProperty("total_excel_row").GetInt32(),r.GetProperty("no_total_row").GetBoolean());
    }
    private JsonElement Case(string name)=>fixture.Oracle.RootElement.EnumerateArray().Single(c=>c.GetProperty("name").GetString()==name);
    private TrialBalanceReview Parse(JsonElement c,TrialBalanceInput? input=null,TrialBalanceLimits? limits=null)=>TrialBalanceParser.Parse(PathOf(c),Path.GetExtension(PathOf(c)),input??Input(c),limits,Ct);
    private static void SameAmount(string expected,string actual)=>Assert.Equal(0,UploadExactAmount.Parse(expected).CompareTo(UploadExactAmount.Parse(actual)));
    private static void SameTotals(JsonElement expected,IReadOnlyDictionary<string,string> actual)
    {Assert.Equal(6,actual.Count);foreach(var p in expected.EnumerateObject()) SameAmount(p.Value.GetString()!,actual[p.Name]);}
    private static string[] Issues(JsonElement rows)=>rows.EnumerateArray().Select(e=>
        (e.GetProperty("excel_row").ValueKind==JsonValueKind.Null?"global":e.GetProperty("excel_row").GetInt32().ToString(CultureInfo.InvariantCulture))+"/"+e.GetProperty("field").GetString()!.ToLowerInvariant()).Order().ToArray();
    private static string[] Issues(IEnumerable<TrialBalanceIssue> rows)=>rows.Select(e=>(e.ExcelRow?.ToString(CultureInfo.InvariantCulture)??"global")+"/"+e.Field.ToLowerInvariant()).Order().ToArray();

    [Fact]
    public void LegacyOracleMatchesRowsHierarchyControlsDifferencesAndRejectedRequests()
    {
        int matched=0,rejected=0;
        foreach(var c in fixture.Oracle.RootElement.EnumerateArray().Where(c=>!c.GetProperty("native_only").GetBoolean()))
        {
            string name=c.GetProperty("name").GetString()!;
            if(c.GetProperty("reject").ValueKind!=JsonValueKind.Null)
            {Assert.Equal(c.GetProperty("reject").GetString(),Assert.Throws<TrialBalanceRejected>(()=>Parse(c)).Code);rejected++;continue;}
            var expected=c.GetProperty("expected");var actual=Parse(c);Assert.False(actual.CanImport);
            Assert.True(expected.GetProperty("valid").GetBoolean()==actual.Valid,name);
            Assert.Equal(c.GetProperty("sha256").GetString(),actual.Sha256);
            Assert.Equal(expected.GetProperty("account_count").GetInt32(),actual.AccountCount);
            Assert.Equal(expected.GetProperty("leaf_count").GetInt32(),actual.LeafCount);
            Assert.Equal(expected.GetProperty("parent_count").GetInt32(),actual.ParentCount);
            Assert.Equal(Issues(expected.GetProperty("errors")),Issues(actual.Errors));Assert.Equal(Issues(expected.GetProperty("warnings")),Issues(actual.Warnings));
            Assert.All(actual.Errors,e=>Assert.Equal("error",e.Severity));Assert.All(actual.Warnings,e=>Assert.Equal("warning",e.Severity));
            Assert.Equal(expected.GetProperty("checks").EnumerateArray().Select(e=>e.GetProperty("key").GetString()+"/"+e.GetProperty("status").GetString()),actual.Checks.Select(e=>e.Key+"/"+e.Status));
            Assert.Equal(expected.GetProperty("account_classifications").EnumerateArray().Select(e=>e[0].GetString()+"/"+e[1].GetString()).Order(),actual.Accounts.Select(a=>a.AccountCode+"/"+a.Classification).Order());
            Assert.Equal(expected.GetProperty("excluded_subtotal_rows").EnumerateArray().Select(e=>e.GetInt32()),actual.ExcludedSubtotalRows);
            Assert.Equal(expected.GetProperty("erp_unbalanced").ValueKind==JsonValueKind.Null?(bool?)null:expected.GetProperty("erp_unbalanced").GetBoolean(),actual.ErpUnbalanced);
            SameTotals(expected.GetProperty("source_leaf_totals"),actual.SourceLeafTotals);
            var comparisons=expected.GetProperty("source_total_comparison").EnumerateArray().ToArray();Assert.Equal(comparisons.Length,actual.SourceTotalComparison.Count);
            for(int i=0;i<comparisons.Length;i++)
            {SameAmount(comparisons[i].GetProperty("source_total").GetString()!,actual.SourceTotalComparison[i].SourceTotal);SameAmount(comparisons[i].GetProperty("leaf_total").GetString()!,actual.SourceTotalComparison[i].LeafTotal);SameAmount(comparisons[i].GetProperty("difference").GetString()!,actual.SourceTotalComparison[i].Difference);Assert.Equal(comparisons[i].GetProperty("within_tolerance").GetBoolean(),actual.SourceTotalComparison[i].WithinTolerance);}
            var roll=expected.GetProperty("rollforward_differences").EnumerateArray().ToArray();Assert.Equal(roll.Length,actual.RollforwardDifferences.Count);
            for(int i=0;i<roll.Length;i++)
            {
                Assert.Equal(roll[i].GetProperty("excel_row").GetInt32(),actual.RollforwardDifferences[i].ExcelRow);
                SameAmount(roll[i].GetProperty("difference_net_exact").GetString()!,actual.RollforwardDifferences[i].DifferenceNet!);
                SameAmount(roll[i].GetProperty("closing_dr_adjustment_exact").GetString()!,actual.RollforwardDifferences[i].Amounts["closing_dr_column"]);
                SameAmount(roll[i].GetProperty("closing_cr_adjustment_exact").GetString()!,actual.RollforwardDifferences[i].Amounts["closing_cr_column"]);
            }
            var global=expected.GetProperty("global_adjustments").EnumerateArray().ToArray();Assert.Equal(global.Length,actual.GlobalAdjustments.Count);
            for(int i=0;i<global.Length;i++) foreach(string role in TrialBalanceMapping.AmountRoles)
                SameAmount(global[i].GetProperty(role.Replace("_column","_adjustment",StringComparison.Ordinal)).GetString()!,actual.GlobalAdjustments[i].Amounts[role]);
            if(expected.GetProperty("draft_reconciliation").ValueKind==JsonValueKind.Null) Assert.Null(actual.DraftReconciliation);
            else
            {
                Assert.NotNull(actual.DraftReconciliation);var draft=expected.GetProperty("draft_reconciliation");
                SameTotals(draft.GetProperty("original_totals"),actual.DraftReconciliation.OriginalTotals);SameTotals(draft.GetProperty("adjustment_totals"),actual.DraftReconciliation.AdjustmentTotals);SameTotals(draft.GetProperty("adjusted_totals"),actual.DraftReconciliation.AdjustedTotals);
            }
            Assert.Equal(c.GetProperty("sha256").GetString(),Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(PathOf(c)))).ToLowerInvariant());matched++;
        }
        Assert.True(matched>=25);Assert.True(rejected>=10);
        File.WriteAllText(Path.Combine(fixture.DirectoryPath,"parity-result.json"),JsonSerializer.Serialize(new {matched,rejected,passed=true}));
    }

    [Fact]
    public void OriginalEvidenceAndCorrectedDraftAreSeparateAndNeverAuthorizeImport()
    {
        var c=Case("date-corrected");var review=Parse(c);var source=review.SourceRows.Single();var account=Assert.Single(review.Accounts);
        Assert.Equal(WorkbookCellKind.DateTime,source.Cells[0].Kind);Assert.Equal("000-01",account.AccountCode);Assert.NotEqual(account.AccountCode,account.SourceAccountCode);
        var difference=Parse(Case("advisory-differences"));SameAmount("90",difference.Accounts[0].Amounts["closing_dr_column"]);
        SameAmount("90",difference.SourceLeafTotals["closing_dr_column"]);SameAmount("100",difference.DraftReconciliation!.AdjustedTotals["closing_dr_column"]);
        Assert.Equal("90",difference.SourceRows[0].Cells[6].NumberToken);Assert.False(difference.CanImport);
        var ignored=Parse(Case("mapped-only"));Assert.Equal(2,ignored.SourceRows.Count);Assert.Single(ignored.Accounts);
        Assert.Equal("not an account",ignored.SourceRows[1].Cells[8].Text);
        Assert.Equal("1",Parse(Case("padding-hierarchy")).Accounts.Single(a=>a.AccountCode=="01-002").ParentAccountCode);
        Assert.Null(Parse(Case("ambiguous-padding")).Accounts.Single(a=>a.AccountCode=="001-02").ParentAccountCode);
        Assert.Equal("1",Parse(Case("raw-prefix-wins")).Accounts.Single(a=>a.AccountCode=="1-02").ParentAccountCode);
        Assert.Empty(Parse(Case("tolerance-at")).RollforwardDifferences);Assert.Single(Parse(Case("tolerance-over")).RollforwardDifferences);
        Assert.NotEqual(Parse(Case("date-code")).InputFingerprint,review.InputFingerprint);
    }

    [Fact]
    public void UnsafePrecisionFormulaCachesAndAmbiguousHeadersRequireReview()
    {
        var precision=Parse(Case("precision"));Assert.False(precision.Valid);Assert.Contains(precision.Errors,e=>e.Code=="precision_review_required");
        Assert.Equal("9007199254740993",precision.Accounts[0].Amounts["opening_dr_column"]);Assert.Equal("9007199254740993",precision.SourceRows[0].Cells[2].Text);
        foreach(string name in new[]{"aggregate-precision","rollforward-precision"})
        {var unsafeReview=Parse(Case(name));Assert.False(unsafeReview.Valid);Assert.Null(unsafeReview.DraftReconciliation);Assert.Contains(unsafeReview.Errors,e=>e.Code=="precision_review_required");}
        Assert.Contains(Parse(Case("out-of-range")).Errors,e=>e.Code=="invalid_amount");
        var unsafeCode=Parse(Case("numeric-code-precision"));Assert.False(unsafeCode.Valid);Assert.Contains(unsafeCode.Errors,e=>e.Code=="numeric_code_review_required");
        Assert.Equal("9007199254740991.1",unsafeCode.SourceRows[0].Cells[0].NumberToken);
        var missing=Parse(Case("missing-formula"));Assert.False(missing.Valid);Assert.Contains(missing.Errors,e=>e.Code=="invalid_amount");
        Assert.Equal(WorkbookFormulaState.MissingResult,missing.SourceRows[0].Cells[2].FormulaState);
        Assert.Equal("ambiguous_mapping",Assert.Throws<TrialBalanceRejected>(()=>Parse(Case("duplicate-headers"))).Code);
        var headers=new[]{new TrialBalanceHeader(1,"Account Code",false),new TrialBalanceHeader(2,"Ledger Code",false),new TrialBalanceHeader(3,"",true)};
        Assert.Null(TrialBalanceMapping.Suggest(headers)["account_code_column"]);
        Assert.All(TrialBalanceMapping.Suggest(headers).Values,value=>Assert.Null(value));
    }

    [Fact]
    public async Task SourceLimitsCancellationAndLargeEvidenceAreBounded()
    {
        var c=Case("balanced-control");
        Assert.Throws<WorkbookReadException>(()=>Parse(c,limits:new(MaximumDataRows:2)));
        Assert.Equal("evidence_capacity",Assert.Throws<TrialBalanceRejected>(()=>Parse(c,limits:new(MaximumEvidenceBytes:100))).Code);
        Assert.Equal("issue_capacity",Assert.Throws<TrialBalanceRejected>(()=>Parse(Case("no-accounts"),limits:new(MaximumIssues:1))).Code);
        Assert.Throws<WorkbookReadException>(()=>Parse(c,limits:new(Workbook:new(MaximumFileBytes:10))));
        Assert.Equal("invalid_limits",Assert.Throws<TrialBalanceRejected>(()=>Parse(c,limits:new(MaximumDataRows:100001))).Code);
        string path=Path.Combine(fixture.DirectoryPath,"large.xlsx");var input=Input(c) with {NoTotalRow=true,TotalExcelRow=null};
        using(var cancelled=new CancellationTokenSource())
        {cancelled.Cancel();Assert.ThrowsAny<OperationCanceledException>(()=>TrialBalanceParser.Parse(path,".xlsx",input,cancellationToken:cancelled.Token));}
        using(var during=new CancellationTokenSource())
        {during.CancelAfter(TimeSpan.FromMilliseconds(25));await Assert.ThrowsAnyAsync<OperationCanceledException>(()=>Task.Run(()=>TrialBalanceParser.Parse(path,".xlsx",input,cancellationToken:during.Token)));}
        Assert.Equal("parsing_timeout",Assert.Throws<TrialBalanceRejected>(()=>TrialBalanceParser.Parse(path,".xlsx",input,new(TimeoutSeconds:1),Ct)).Code);
        var timer=Stopwatch.StartNew();var review=TrialBalanceParser.Parse(path,".xlsx",input,cancellationToken:Ct);timer.Stop();
        Assert.True(review.Valid);Assert.False(review.CanImport);Assert.Equal(15000,review.AccountCount);Assert.Equal(15000,review.SourceRows.Count);
        SameAmount("9375",review.SourceLeafTotals["opening_dr_column"]);SameAmount("9375",review.SourceLeafTotals["closing_cr_column"]);
        long bytes=JsonSerializer.SerializeToUtf8Bytes(review).LongLength;Assert.True(bytes<=33554432);
        File.WriteAllText(Path.Combine(fixture.DirectoryPath,"large-result.json"),JsonSerializer.Serialize(new {rows=review.AccountCount,milliseconds=timer.ElapsedMilliseconds,bytes,passed=true}));
    }
}
