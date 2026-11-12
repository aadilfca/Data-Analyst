namespace AuditPlatform.Api.Uploads;

public sealed record CorrectionRequest(CoverageRequest Coverage,string? ExpectedCorrectionKey=null,int Page=1,int PageSize=50);
public sealed record CorrectionCounts(int Incoming,int Existing,int Unchanged,int Changed,int Added,int Removed,int Retained,
    int InformationalUpdates,int SourceOrderUpdates);
public sealed record CorrectionIssue(string Kind,string DocumentGroup,int Incoming,int Existing,IReadOnlyList<string> Fields);
public sealed record CorrectionRow(string Action,int? Row,long? ExistingId,string DocumentGroup,string Identity,long? ExpectedVersion,
    IReadOnlyList<string> ChangedFields);
public sealed record CorrectionOutput(string Status,CoverageOutput Coverage,string CorrectionKey,string StoreKey,CorrectionCounts Counts,
    bool CorrectionChecksPassed,bool NoTransactionChanges,bool NoOp,bool CanImport,int Page,int PageSize,int TotalRows,
    IReadOnlyList<CorrectionRow> Rows,int IssueCount,IReadOnlyList<CorrectionIssue> Issues,bool DetailsTruncated,IReadOnlyList<string> Warnings);
public sealed record CorrectionResult(string Status,CorrectionOutput? Output=null,string? Code=null);

// Supplied only by trusted server metadata or isolated parity fixtures. Request
// payloads cannot choose line/account fields or authorize weak single-row pairing.
public sealed record CorrectionBindings(string? AccountKey=null,string? DebitKey=null,string? CreditKey=null,
    bool AllowSingleFallback=false,bool RequiresBindingReview=false);
