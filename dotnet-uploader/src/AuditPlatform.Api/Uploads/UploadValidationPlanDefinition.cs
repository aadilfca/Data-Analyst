namespace AuditPlatform.Api.Uploads;

public sealed record ValidationPlanRequest(Guid RequestId,CorrectionRequest Review);
public sealed record ValidationPlanCheck(string PlanId,string Signature,CorrectionRequest Review);
public sealed record ValidationPlanOutput(string Status,Guid RequestId,string? PlanId,string? Signature,DateTimeOffset ExpiresAt,
    CorrectionCounts Counts,string CorrectionKey,bool CanImport=false);
public sealed record ValidationPlanResult(string Status,ValidationPlanOutput? Output=null,string? Code=null);
public sealed record ValidationPlanGuard(long Id,string Hash);
public sealed record ValidationPlanAction(CorrectionRow Action,IReadOnlyDictionary<string,IdentityAtom>? Values,string? ValidatedDate);
public sealed record ValidationPlanPayload(string Format,CorrectionRequest Review,string RequestHash,string CorrectionKey,string StoreKey,
    CorrectionCounts Counts,IReadOnlyList<ValidationPlanAction> Actions,IReadOnlyList<ValidationPlanGuard> Guards,
    IReadOnlyList<string> IncomingGroups,IReadOnlyList<string> ChangedGroups);
public sealed record ValidationPlanEnvelope(string Format,Guid RequestId,string PlanId,int Tenant,int User,int Client,int Template,int File,
    string RequestHash,string PayloadHash,string CorrectionKey,string ConfigurationKey,long MappingRevision,string FileHash,
    DateTimeOffset CreatedAt,DateTimeOffset ExpiresAt,bool NoOp,CorrectionCounts Counts);
public sealed record StoredValidationPlan(ValidationPlanEnvelope Envelope,string Signature,string Payload,string State);
internal sealed class ValidationPlanRejected(string code) : Exception(code) {public string Code {get;}=code;}
