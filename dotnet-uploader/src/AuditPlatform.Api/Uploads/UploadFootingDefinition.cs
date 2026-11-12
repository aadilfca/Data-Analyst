namespace AuditPlatform.Api.Uploads;

public sealed record FootingRequest(InputValidationRequest Validation,bool ControlTotalConfirmed=false,
    bool ConfirmVariance=false,string? ExpectedCheckKey=null);
public sealed record FootingColumnCheck(int ColumnId,string StorageKey,string Column,int SourcePosition,
    string ExcelTotal,string DetailTotal,string Difference,string Status);
public sealed record FootingSummary(int RowCount,int DocumentCount,int MissingDocumentRows,IReadOnlyDictionary<int,string> Totals,bool TotalsComplete);
public sealed record FootingIssue(int Row,int? ColumnId,int? SourcePosition,string Kind,string Message);
public sealed record FootingOutput(string Status,PreparationContext Context,int? ControlTotalRow,string CheckKey,
    bool FootingPassed,bool CanImport,FootingSummary FullSheet,FootingSummary SelectedDetail,
    IReadOnlyList<FootingColumnCheck> Checks,InputValidationOutput InputValidation,int IssueCount,
    IReadOnlyList<FootingIssue> Issues,bool DetailsTruncated,bool VarianceConfirmed,IReadOnlyList<string> Warnings);
public sealed record FootingResult(string Status,FootingOutput? Output=null,string? Code=null);
