using AuditPlatform.Api.Workbooks;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Uploads;

// Passing this boundary is not approval to import or replace a period.
public sealed record InputValidationRequest(PreparationRequest Source,string ComparisonMode="correction",
    [property:JsonNumberHandling(JsonNumberHandling.Strict)] int? ControlTotalRow=null,
    int IssuePage=1,int IssuePageSize=50);
public sealed record InputCellIssue(int? ColumnId,string? Column,string? StorageKey,int? SourcePosition,string Kind,string Message);
public sealed record InputRowIssue(int Row,string Kind,IReadOnlyList<InputCellIssue> Errors,
    IReadOnlyList<InputCellIssue> Missing,bool ForceIncluded=false,int? ParentRow=null);
public sealed record InputValidationCounts(int SourceRows,int TransactionRows,int ControlRows,int ContinuationRows,
    int ExcludedRows,int ErrorRows,int RejectedRows,int ForceIncludedRows,int EligibleRows);
public sealed record InputValidationOutput(PreparationContext Context,string ComparisonMode,int? ControlTotalRow,
    string ValidationKey,bool ChecksCompleted,bool InputChecksPassed,bool CanImport,InputValidationCounts Counts,
    int IssueRows,int IssuePage,int IssuePageSize,IReadOnlyList<InputRowIssue> Issues,bool DetailsTruncated,IReadOnlyList<string> Warnings);
public sealed record InputValidationResult(string Status,InputValidationOutput? Output=null,string? Code=null);
// Request-local notification only; consumers must process and discard the row.
public sealed record CheckedSourceRow(WorkbookRow Row,string Kind,bool Excluded,bool Eligible,bool HasErrors);
