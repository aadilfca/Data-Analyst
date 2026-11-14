using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class TrialBalanceReviewOptions
{
    public const string SectionName="TrialBalanceReview";
    public bool Enabled {get;set;}
    public bool UiEnabled {get;set;}
    public string SigningKey {get;set;}="";
    [Range(1,60)] public int TimeoutSeconds {get;set;}=60;
    [Range(1,5)] public int ProofMinutes {get;set;}=5;
    [Range(1,100000)] public int MaximumSnapshotRows {get;set;}=100000;
    [Range(1024,33554432)] public int MaximumSnapshotBytes {get;set;}=33554432;
    [Range(1024,1048576)] public int MaximumResponseBytes {get;set;}=1048576;
    [Range(1,1000)] public int MaximumChanges {get;set;}=1000;
    [Range(1,1024)] public int MaximumOperationsPerClient {get;set;}=1024;
    public bool Valid()
    {
        if(!Validator.TryValidateObject(this,new(this),null,true)||UiEnabled&&!Enabled)return false;
        if(!Enabled)return true;
        try{return Convert.FromBase64String(SigningKey).Length is >=32 and <=64;}catch(FormatException){return false;}
    }
}
