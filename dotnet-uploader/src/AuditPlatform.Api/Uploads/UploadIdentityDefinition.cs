namespace AuditPlatform.Api.Uploads;

public sealed record IdentityRequest(CoverageRequest Coverage,string? ExpectedIdentityKey=null,int Page=1,int PageSize=50);
public sealed record IdentityIssue(int Row,string Kind,string Message);
public sealed record IdentityRowCheck(int Row,string DocumentGroup,string Identity,long? ExistingId,string Status);
public sealed record IdentityCounts(int IncomingRows,int ExistingRows,int MatchedRows,int UnmatchedIncoming,int UnmatchedExisting,
    int RepeatedGroups,int RepeatedRows,int DuplicateLineRows,int InformationalUpdates,int SourceOrderUpdates);
public sealed record IdentityOutput(string Status,CoverageOutput Coverage,string IdentityKey,string StoreKey,IdentityCounts Counts,
    bool IdentityChecksPassed,bool NoTransactionChanges,bool NoOp,bool CanImport,int Page,int PageSize,
    IReadOnlyList<IdentityRowCheck> Rows,int IssueCount,IReadOnlyList<IdentityIssue> Issues,bool DetailsTruncated,IReadOnlyList<string> Warnings);
public sealed record IdentityResult(string Status,IdentityOutput? Output=null,string? Code=null);

// Request-local scalar views; raw cells and canonical JSON are never replaced.
public sealed record IdentityAtom(string Kind,string? Text=null);
public sealed record IdentityColumn(string Key,string Type,bool Compare=true,bool Description=false);
public sealed record IdentityProfile(int TemplateId,IReadOnlyList<IdentityColumn> Columns,string DocumentKey,string? DateKey=null,
    string? LineKey=null,string? AccountCode=null,UploadDerivedProfile? Derivation=null);
public sealed record IdentitySourceRow(int Row,IReadOnlyDictionary<string,IdentityAtom> Values,string? PrimaryDate=null);
public sealed record IdentityStoredRow(long Id,IReadOnlyDictionary<string,IdentityAtom> Values,string? DocumentGroup,string? Identity,
    long Version=1,int? SourceRow=null,int? OrderUpload=null,string? PrimaryDate=null,string? DerivedHash=null);
public sealed class IdentityReview(string code) : Exception(code) {public string Code {get;}=code;}

// Disposable inputs shared by identity and correction review, never an import plan.
internal sealed record IdentityReadResult<T>(string Status,T? Output=default,string? Code=null);
internal sealed record IdentityReadContext(IdentityProfile Profile,IReadOnlyList<IdentitySourceRow> Incoming,
    IReadOnlyList<IdentityStoredRow> Existing,CoverageOutput Coverage,string StoreKey,
    IReadOnlyDictionary<string,List<long>> Collisions,IReadOnlySet<string> IncompleteGroups,bool SourceFailed);
public sealed record IdentityMatches(string[] Identities,string[] IncomingGroups,string[] ExistingGroups,
    IReadOnlyDictionary<int,int> Matched,IReadOnlySet<int> UnmatchedExisting);
