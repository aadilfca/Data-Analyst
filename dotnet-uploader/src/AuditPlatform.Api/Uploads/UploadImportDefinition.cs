namespace AuditPlatform.Api.Uploads;

public sealed record UploadImportRequest(Guid OperationId,string PlanId,string Signature,CorrectionRequest Review);
public sealed record UploadImportOutput(Guid OperationId,string PlanId,int FileId,CorrectionCounts Counts,DateTimeOffset ImportedAt,Guid? ReceiptId=null,string? ControlStatus=null);
public sealed record UploadImportResult(string Status,UploadImportOutput? Output=null,string? Code=null);
