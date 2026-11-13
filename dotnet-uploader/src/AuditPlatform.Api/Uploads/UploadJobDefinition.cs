using System.Text.Json;
using AuditPlatform.Api.Data;

namespace AuditPlatform.Api.Uploads;

public sealed record UploadJobRequest(Guid RequestId,string Kind,CorrectionRequest Review,string? PlanId=null,string? Signature=null,int Format=1);
public sealed record UploadJobCompletion(Guid ReceiptId,string ControlStatus,UploadImportOutput? Import=null);
public sealed record UploadJobOutput(Guid Id,int FileId,string State,string Stage,int Attempts,int MaximumAttempts,
    DateTimeOffset ExpiresAt,bool SourceReady,string? Code,UploadJobCompletion? Completion);
public sealed record UploadJobResult(string Status,UploadJobOutput? Job=null,string? Code=null);

internal sealed record UploadJobData(Guid Id,int Tenant,int User,int Client,int Template,int File,UploadJobRequest Request,
    string Digest,string SessionHash,long ContextVersion,string State,string Stage,int Attempts,long Version,Guid? Worker,
    DateTimeOffset? Lease,DateTimeOffset Expires,DateTimeOffset SourceExpires,long Bytes,string SourceHash,bool SourceReady,
    string? Code,UploadJobCompletion? Completion,string StorageHash);

// Server-only execution capability. HTTP input cannot supply a lease or bypass guards.
internal sealed record UploadJobExecution(UploadJobData Job,UploadJobStore Store)
{
    internal Task GuardAsync(PostgresTransactionContext db,CancellationToken ct)=>Store.GuardAsync(db,Job,ct);
    internal Task CompleteAsync(PostgresTransactionContext db,UploadJobCompletion result,CancellationToken ct)=>Store.CompleteAsync(db,Job,result,ct);
}
internal sealed record UploadJobCleanup(Guid Id,long Version,string StorageHash);

internal sealed class UploadJobRejected(string code):Exception(code)
{
    public string Code {get;}=code;
}
