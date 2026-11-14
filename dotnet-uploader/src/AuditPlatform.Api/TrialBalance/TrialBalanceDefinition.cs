using System.Text.Json;
using AuditPlatform.Api.Workbooks;

namespace AuditPlatform.Api.TrialBalance;

// P34 is an unhosted, non-writing review kernel. Its result never authorizes import.
public sealed record TrialBalanceInput(IReadOnlyDictionary<string,string?> Mapping,
    IReadOnlyDictionary<int,string>? CodeOverrides=null,IReadOnlyDictionary<int,string>? ClassificationOverrides=null,
    IReadOnlyList<int>? ExcludedExcelRows=null,int? TotalExcelRow=null,bool NoTotalRow=false);
public sealed record TrialBalanceLimits(int MaximumDataRows=100000,long MaximumEvidenceBytes=33554432,
    int MaximumIssues=10000,int TimeoutSeconds=60,WorkbookReadLimits? Workbook=null);
public sealed record TrialBalanceHeader(int Position,string Label,bool Ambiguous);
public sealed record TrialBalanceSourceRow(int ExcelRow,IReadOnlyList<WorkbookCell> Cells);
public sealed record TrialBalanceIssue(int? ExcelRow,string Field,string Code,string Message,string Severity="error");
public sealed record TrialBalanceAccount(int ExcelRow,string SourceAccountCode,string AccountCode,string? AccountName,
    string Classification,string AutoClassification,string? ParentAccountCode,IReadOnlyDictionary<string,string> Amounts);
public sealed record TrialBalanceControl(string Field,string SourceTotal,string LeafTotal,string Difference,bool WithinTolerance);
public sealed record TrialBalanceCheck(string Key,string Status,int AffectedRows=0);
public sealed record TrialBalanceDifference(int? ExcelRow,string? AccountCode,string? AccountName,
    IReadOnlyDictionary<string,string> Amounts,bool Global=false,string? ExpectedClosingNet=null,
    string? OriginalClosingNet=null,string? DifferenceNet=null);
public sealed record TrialBalancePair(string Difference,bool WithinTolerance);
public sealed record TrialBalanceReconciliation(IReadOnlyDictionary<string,string> OriginalTotals,
    IReadOnlyDictionary<string,string> AdjustmentTotals,IReadOnlyDictionary<string,string> AdjustedTotals,
    IReadOnlyDictionary<string,TrialBalancePair> PairDifferences);
public sealed record TrialBalanceReview(string Sheet,string Sha256,string InputFingerprint,
    IReadOnlyList<TrialBalanceHeader> Headers,IReadOnlyDictionary<string,string?> SuggestedMapping,
    IReadOnlyList<TrialBalanceSourceRow> SourceRows,IReadOnlyList<TrialBalanceAccount> Accounts,
    IReadOnlyList<TrialBalanceIssue> Errors,IReadOnlyList<TrialBalanceIssue> Warnings,
    IReadOnlyList<TrialBalanceCheck> Checks,IReadOnlyList<TrialBalanceControl> SourceTotalComparison,
    IReadOnlyDictionary<string,string> SourceLeafTotals,IReadOnlyList<TrialBalanceDifference> RollforwardDifferences,
    IReadOnlyList<TrialBalanceDifference> GlobalAdjustments,TrialBalanceReconciliation? DraftReconciliation,
    IReadOnlyList<int> ExcludedSubtotalRows,int? TotalExcelRow,bool NoTotalRow,bool? ErpUnbalanced)
{
    public bool Valid=>Errors.Count==0;
    public bool CanImport=>false;
    public int AccountCount=>Accounts.Count;
    public int LeafCount=>Accounts.Count(a=>a.Classification=="leaf");
    public int ParentCount=>AccountCount-LeafCount;
    public string Tolerance=>"0.01";
}
public sealed class TrialBalanceRejected(string code,string message) : Exception(message)
{public string Code {get;}=code;}

internal static class TrialBalanceBudget
{
    // Count serialized evidence without allocating an unbounded result buffer.
    private sealed class Counter(long maximum) : Stream
    {
        public override bool CanRead=>false;public override bool CanSeek=>false;public override bool CanWrite=>true;
        public override long Length=>Position;public override long Position {get;set;}
        public override void Write(byte[] buffer,int offset,int count)=>Write(buffer.AsSpan(offset,count));
        public override void Write(ReadOnlySpan<byte> buffer)
        {if(buffer.Length>maximum-Position) throw new TrialBalanceRejected("evidence_capacity","Trial Balance evidence exceeds its limit.");Position+=buffer.Length;}
        public override void Flush() { }
        public override int Read(byte[] buffer,int offset,int count)=>throw new NotSupportedException();
        public override long Seek(long offset,SeekOrigin origin)=>throw new NotSupportedException();
        public override void SetLength(long value)=>throw new NotSupportedException();
    }
    internal static long Size<T>(T value,long remaining)
    {using var stream=new Counter(remaining);JsonSerializer.Serialize(stream,value);return stream.Length;}
}
