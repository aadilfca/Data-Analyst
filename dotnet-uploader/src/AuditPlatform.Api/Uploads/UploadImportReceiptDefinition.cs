namespace AuditPlatform.Api.Uploads;

public sealed record ReceiptControlColumn(int Id,string Key,string Name);
public sealed record ReceiptSourceControls(FootingSummary FullSheet,FootingSummary ApprovedDetail,IReadOnlyList<ReceiptControlColumn> Footings,
    IReadOnlyList<FootingColumnCheck> FooterChecks,string DocumentKey,bool CompletePeriod,string? Start,string? End,int? ControlRow,bool VarianceConfirmed);
public sealed record ReceiptSourceRow(int Row,long? ExistingId,string Group,string Action);
public sealed record ReceiptMetric(string Key,string Label,string? Source,string? Stored,string? Difference,bool? Matches);
public sealed record ReceiptReconciliation(string Status,string Message,bool FootingConfigured,DateTimeOffset CheckedAt,IReadOnlyList<ReceiptMetric> Metrics);
public sealed record ReceiptColumn(int Id,string Key,string Name,string Type,int Order);
public sealed record ReceiptGroup(string Group,string BeforeHash,string AfterHash,IReadOnlyList<int> IncomingIds,IReadOnlyList<int> AddedIds);
public sealed record UploadImportReceipt(Guid Id,string Kind,int FileId,int ClientId,int TemplateId,string SourceHash,string Sheet,
    DateTimeOffset CompletedAt,DateTimeOffset EvidenceExpiresAt,CorrectionCounts Counts,ReceiptSourceControls Source,
    ReceiptReconciliation Reconciliation,IReadOnlyList<ReceiptColumn> Columns,IReadOnlyList<ReceiptGroup> Groups,string EvidenceStatus="available");
public sealed record ReceiptResult(string Status,UploadImportReceipt? Receipt=null,string? Code=null);
public sealed record ReceiptEvidenceRow(int Id,string Group,string? Date,string Raw,string Guard,int? SourceRow=null,int? UploadId=null,int? OrderUpload=null);
public sealed record ReceiptWorkbookData(UploadImportReceipt Receipt,IReadOnlyList<ReceiptEvidenceRow> Previous,IReadOnlyList<ReceiptEvidenceRow> Incoming);
public sealed record ReceiptWorkbookResult(string Status,byte[]? Bytes=null,string? Code=null);
