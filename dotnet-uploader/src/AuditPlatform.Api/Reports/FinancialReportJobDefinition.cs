namespace AuditPlatform.Api.Reports;

public sealed record FinancialReportJobRequest(Guid RequestId,int ClientId,string Report,int? TrialBalanceUploadId=null,
    string? PeriodStart=null,string? PeriodEnd=null,string GroupBy="month",string TreasuryView="combined",
    IReadOnlyList<string>? SelectedTreasuryAccountCodes=null,string Search="",string Category="",
    string AccountHead="",string TreasuryType="",string PeriodKey="",string SummaryFilter="",
    bool HideZeroRows=false,string? ExpectedFingerprint=null,string? ExpectedIdentity=null);

public sealed record FinancialReportJobOutput(Guid Id,int ClientId,string Report,string State,
    int Attempt,DateTimeOffset ExpiresAt,string? Code,string? FileName,int? ArtifactBytes)
{
    public string Stage { get; init; } = "queued";
    public int ProgressCurrent { get; init; }
    public int ProgressTotal { get; init; }
    public bool SourceReady { get; init; }
}

public sealed record FinancialReportJobResult(string Status,FinancialReportJobOutput? Job=null,string? Code=null);

internal sealed record FinancialReportJobData(Guid Id,int TenantId,int UserId,int ClientId,long ContextVersion,
    string SessionHash,FinancialReportJobRequest Request,string InputFingerprint,string State,int Attempts,
    Guid? LeaseId,DateTimeOffset? LeaseUntil,DateTimeOffset ExpiresAt,string? Code,string? FileName,
    byte[]? Artifact,string? ArtifactHash,string Stage,int ProgressCurrent,int ProgressTotal,bool SourceReady);
internal sealed record FinancialReportJobLease(FinancialReportJobData Job,AuditPlatform.Api.Authentication.VerifiedSession Actor);
public sealed record FinancialReportJobDownload(byte[] Bytes,string FileName,string ContentType);
