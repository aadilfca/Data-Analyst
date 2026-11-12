using System.ComponentModel.DataAnnotations;
namespace AuditPlatform.Api.Configuration;

public sealed class UploadValidationPlanOptions
{
    public const string SectionName="UploadValidationPlans";
    public bool Enabled {get;set;}
    public string SigningKey {get;set;}="";
    [Range(1,8)] public int MaximumConcurrentRequests {get;set;}=2;
    [Range(1,300)] public int TimeoutSeconds {get;set;}=60;
    [Range(4096,4194304)] public int MaximumPayloadBytes {get;set;}=4194304;
    [Range(1,100000)] public int MaximumRows {get;set;}=100000;
    [Range(1,128)] public int MaximumReceiptsPerScope {get;set;}=128;
    [Range(1,500000000)] public long MaximumDatabaseBytes {get;set;}=500000000;
    public bool ValidKey()=>!Enabled||KeyBytes() is {Length:>=32 and <=64};
    public byte[]? KeyBytes() {try{return Convert.FromBase64String(SigningKey);}catch(FormatException){return null;}}
}
