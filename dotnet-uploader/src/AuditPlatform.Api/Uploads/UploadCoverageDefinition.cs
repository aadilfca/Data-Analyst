namespace AuditPlatform.Api.Uploads;

// Disposable evidence only. P24 must independently bind an import plan to this scope.
public sealed record CoverageRequest(FootingRequest Footing,string? CoverageStart=null,string? CoverageEnd=null,
    bool ConfirmReportCoverage=false,bool ConfirmEmptyPeriod=false,string? LedgerAccountCode=null,
    string? LedgerPeriodStart=null,string? LedgerPeriodEnd=null,string? ExpectedCoverageKey=null);
public sealed record CoveragePeriod(string Start,string End);
public sealed record CoverageLedger(int UploadId,string AccountCode,string AccountName,string ReferenceKey);
public sealed record CoverageDateSummary(string Status,int ColumnId,int? SourcePosition,string? Start,string? End,
    int TransactionDates,int SelectedDates,int MissingDates,int InvalidDates,int OutsideRows);
public sealed record CoverageIssue(int Row,string Kind,string Message);
public sealed record CoverageOutput(string Status,PreparationContext Context,string CoverageKey,string ComparisonMode,
    CoveragePeriod? DeclaredPeriod,CoverageLedger? Ledger,CoverageDateSummary? DateSuggestion,
    bool CoverageChecksPassed,bool CompletePeriodDeclared,bool EmptyPeriodConfirmed,bool CanImport,
    FootingOutput Footing,int IssueCount,IReadOnlyList<CoverageIssue> Issues,bool DetailsTruncated,IReadOnlyList<string> Warnings);
public sealed record CoverageResult(string Status,CoverageOutput? Output=null,string? Code=null);
