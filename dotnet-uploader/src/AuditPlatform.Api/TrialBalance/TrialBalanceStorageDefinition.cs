using System.Text.Json;

namespace AuditPlatform.Api.TrialBalance;

public sealed record TrialBalancePeriod(string Start,string End,string Type);
// Server-owned file input, never an HTTP path/body contract. No file is retained
// by this storage kernel; the future host must establish staging ownership.
public sealed record TrialBalanceStorageSource(string Path,string Extension,string FileName,TrialBalanceInput Review);
public sealed record TrialBalanceStorageRequest(Guid OperationId,string PreviewToken,string Confirmation);
public sealed record TrialBalanceStoredUpload(int Id,string FileName,string Status,int AccountCount,string? Start,string? End,bool NativeOwned);
public sealed record TrialBalanceStoragePreview(int ClientId,string Kind,TrialBalancePeriod? Period,int? UploadId,
    string Sha256,string ReviewFingerprint,string Fingerprint,string PreviewToken,bool CanApply,
    IReadOnlyList<string> Blockers,IReadOnlyList<TrialBalanceStoredUpload> Conflicts,int AccountCount,
    int LeafCount,int ParentCount,int ApplicableSavedRows,DateTimeOffset GeneratedAt);
public sealed record TrialBalanceStorageOutcome(Guid OperationId,int ClientId,string Kind,int? UploadId,
    int RemovedUploads,int AccountCount,int LeafCount,int ParentCount,int RowAdjustmentCount,
    int GlobalAdjustmentCount,bool AssignmentsCleared,string Sha256,DateTimeOffset CompletedAt);
public sealed record TrialBalanceStorageResult(string Status,TrialBalanceStoragePreview? Preview=null,
    TrialBalanceStorageOutcome? Outcome=null,IReadOnlyList<TrialBalanceStoredUpload>? Uploads=null,string? Code=null);

internal sealed record TrialBalanceStorageActor(int Tenant,int User,string Name);
internal sealed record TrialBalanceStorageProof(int Version,int Tenant,int User,long Context,int Client,string Kind,
    TrialBalancePeriod? Period,int? Upload,string SourceName,string SourceExtension,string Sha256,string Review,string Fingerprint,DateTimeOffset Expires);
internal sealed record TrialBalanceStorageSnapshot(string Fingerprint,IReadOnlyList<TrialBalanceStoredUpload> Uploads,
    IReadOnlyList<string> Blockers,IReadOnlyList<string> Tables,int[] UploadIds,int[] AccountIds,
    int ClientUploadCount,int OperationCount,int AccountCount,Dictionary<string,string> AmountTypes);
internal sealed class TrialBalanceStorageRejected(string status,string code) : Exception(code)
{internal string Status {get;}=status;internal string Code {get;}=code;}
internal static class TrialBalanceStorageJson
{internal static readonly JsonSerializerOptions Options=new(JsonSerializerDefaults.Web){PropertyNamingPolicy=JsonNamingPolicy.SnakeCaseLower};}
