using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.TrialBalance;

public sealed record TrialBalanceReviewQuery(int ClientId,int UploadId=0,int Page=1,int Limit=100,string Search="",bool? IsLeaf=null);
public sealed record TrialBalanceReviewChange([property:JsonPropertyName("account_id")]int AccountId,
    [property:JsonPropertyName("expected_account_code")]string ExpectedAccountCode,[property:JsonPropertyName("account_code")]string? AccountCode=null,
    [property:JsonPropertyName("is_leaf")]bool? IsLeaf=null);
public sealed record TrialBalanceReviewRequest([property:JsonPropertyName("operation_id")]Guid OperationId,
    [property:JsonPropertyName("review_token")]string ReviewToken,[property:JsonPropertyName("changes")]IReadOnlyList<TrialBalanceReviewChange> Changes);
public sealed record TrialBalanceReviewOutcome(Guid OperationId,int ClientId,int UploadId,string Kind,bool Updated,
    int CorrectedCount,int ClassificationCount,int HierarchyCount,string BeforeHash,string AfterHash,DateTimeOffset CompletedAt);
public sealed record TrialBalanceReviewResult(string Status,JsonElement? Value=null,TrialBalanceReviewOutcome? Outcome=null,string? Code=null);

internal sealed record TrialBalanceReviewProof(int Version,int Tenant,int User,long Context,int Client,int Upload,
    string Purpose,string Fingerprint,string ChangesHash,DateTimeOffset Expires);
internal sealed record TrialBalanceReviewRow(int Id,string Code,string? SourceCode,string? Name,string? Parent,int Level,
    string Classification,bool Manual,int Sort,JsonElement Values);
internal sealed record TrialBalanceReviewSnapshot(TrialBalanceStorageSnapshot Storage,IReadOnlyList<TrialBalanceReviewRow> Rows,
    IReadOnlyList<JsonElement> Adjustments,JsonElement Controls,JsonElement Upload,string ProtectedHash,int Operations,
    IReadOnlyDictionary<string,IReadOnlyList<JsonElement>> Assignments,string Fingerprint,IReadOnlyList<string> Blockers);
internal sealed record TrialBalanceReviewPlanned(int Id,string Code,string? Parent,int Level,string Classification,bool Manual);
internal sealed record TrialBalanceReviewPlan(IReadOnlyList<TrialBalanceReviewPlanned> Rows,IReadOnlyList<(string Source,string Target)> Pairs,
    int Codes,int Classifications,int Hierarchy);
