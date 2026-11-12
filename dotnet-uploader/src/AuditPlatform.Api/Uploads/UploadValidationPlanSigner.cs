using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;
namespace AuditPlatform.Api.Uploads;

public sealed class UploadValidationPlanSigner(IOptions<UploadValidationPlanOptions> options)
{
    public const string Format="dotnet-upload-plan-v1";
    public static CorrectionRequest Normalize(CorrectionRequest review)=>review with {Page=1,PageSize=50,
        Coverage=review.Coverage with {Footing=review.Coverage.Footing with {Validation=review.Coverage.Footing.Validation with {
            Source=review.Coverage.Footing.Validation.Source with {RequestId=Guid.Empty,Operation="rows",Page=1,PageSize=10}}}}};
    public static string RequestHash(int file,CorrectionRequest review)=>UploadIdentityValue.Hash(JsonSerializer.Serialize(new {Format,File=file,Review=Normalize(review)}));
    public static string GuardHash(IdentityStoredRow row)=>UploadIdentityValue.Hash(JsonSerializer.Serialize(new {row.Id,row.Version,row.Identity,
        row.DocumentGroup,row.SourceRow,row.OrderUpload,row.PrimaryDate,Values=row.Values.OrderBy(v=>v.Key,StringComparer.Ordinal)}));
    public string Sign(ValidationPlanEnvelope envelope)
    {
        var key=options.Value.KeyBytes();if(key is not {Length:>=32 and <=64}) throw new ValidationPlanRejected("plan_signing_unavailable");
        try{return Convert.ToHexString(HMACSHA256.HashData(key,Encoding.UTF8.GetBytes(JsonSerializer.Serialize(envelope)))).ToLowerInvariant();}
        finally {CryptographicOperations.ZeroMemory(key);}
    }
    public bool Verify(ValidationPlanEnvelope envelope,string signature)
    {
        if(envelope.Format!=Format||signature.Length!=64) return false;
        try{return CryptographicOperations.FixedTimeEquals(Convert.FromHexString(Sign(envelope)),Convert.FromHexString(signature));}
        catch(FormatException){return false;}
    }
    public bool Verify(StoredValidationPlan plan)=>Verify(plan.Envelope,plan.Signature)&&UploadIdentityValue.Hash(plan.Payload)==plan.Envelope.PayloadHash;
}
