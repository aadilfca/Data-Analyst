namespace AuditPlatform.Api.Reports;

public sealed record FinancialReportJobRequest(Guid RequestId,int ClientId,string Report,int? TrialBalanceUploadId=null,
    string? PeriodStart=null,string? PeriodEnd=null,string GroupBy="month",string TreasuryView="combined");

public sealed record FinancialReportJobOutput(Guid Id,int ClientId,string Report,string State,
    int Attempt,DateTimeOffset ExpiresAt,string? Code,string? FileName,int? ArtifactBytes);

public sealed record FinancialReportJobResult(string Status,FinancialReportJobOutput? Job=null,string? Code=null);

internal sealed record FinancialReportJobData(Guid Id,int TenantId,int UserId,int ClientId,long ContextVersion,
    string SessionHash,FinancialReportJobRequest Request,string InputFingerprint,string State,int Attempts,
    Guid? LeaseId,DateTimeOffset? LeaseUntil,DateTimeOffset ExpiresAt,string? Code,string? FileName,
    byte[]? Artifact,string? ArtifactHash);
internal sealed record FinancialReportJobLease(FinancialReportJobData Job,AuditPlatform.Api.Authentication.VerifiedSession Actor);
public sealed record FinancialReportJobDownload(byte[] Bytes,string FileName,string ContentType);
