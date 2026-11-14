using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

// Unhosted P35 services only. P36 must separately approve registration/routes.
public sealed class TrialBalanceStorageOptions
{
    public bool Enabled {get;set;}
    public string SigningKey {get;set;}="";
    [Range(1,60)] public int TimeoutSeconds {get;set;}=60;
    [Range(1,10)] public int ProofMinutes {get;set;}=5;
    [Range(1,1024)] public int MaximumOperationsPerClient {get;set;}=1024;
    [Range(1,256)] public int MaximumUploadsPerClient {get;set;}=256;
    [Range(1,100000)] public int MaximumSnapshotRows {get;set;}=100000;
    [Range(1024,33554432)] public int MaximumSnapshotBytes {get;set;}=33554432;
    [Range(1024,131072)] public int MaximumResponseBytes {get;set;}=131072;
    [Range(1,256)] public int MaximumCatalogEntries {get;set;}=256;
    public bool Valid()
    {
        if(!Validator.TryValidateObject(this,new(this),null,true)) return false;
        if(!Enabled) return true;
        try{return Convert.FromBase64String(SigningKey).Length is >=32 and <=64;}catch(FormatException){return false;}
    }
}
