using System.ComponentModel.DataAnnotations;

namespace AuditPlatform.Api.Configuration;

public sealed class UploadJobOptions
{
    public const string SectionName="UploadJobs";
    public bool Enabled {get;set;}
    [Range(1,4)] public int MaximumWorkers {get;set;}=2;
    [Range(1,32)] public int MaximumJobsPerOwner {get;set;}=8;
    [Range(1,128)] public int MaximumOutstandingJobs {get;set;}=32;
    [Range(1,256)] public int MaximumRetainedJobsPerScope {get;set;}=128;
    [Range(1,3)] public int MaximumAttempts {get;set;}=3;
    [Range(120,300)] public int LeaseSeconds {get;set;}=120;
    [Range(1,15)] public int HeartbeatSeconds {get;set;}=15;
    [Range(1,10)] public int PollSeconds {get;set;}=2;
    [Range(1,120)] public int TransferTimeoutSeconds {get;set;}=120;
    [Range(1,67108864)] public long MaximumFileBytes {get;set;}=67108864;
    [Range(1,268435456)] public long MaximumSourceBytes {get;set;}=268435456;
    public string StorageUrl {get;set;}="";
    public string StorageBucket {get;set;}="";
    // Supply only through the server secret store/environment. Never serialize options.
    public string StorageKey {get;set;}="";
    public bool ValidStorage()=>!Enabled||(Uri.TryCreate(StorageUrl,UriKind.Absolute,out var uri)
        &&(uri.Scheme=="https"||uri.Scheme=="http"&&uri.IsLoopback)&&uri.UserInfo.Length==0
        &&uri.Query.Length==0&&uri.Fragment.Length==0&&uri.AbsolutePath.Trim('/')==""
        &&StorageBucket is {Length:>0 and <=63}&&StorageBucket.All(c=>char.IsAsciiLetterOrDigit(c)||c=='-')
        &&StorageKey is {Length:>0 and <=4096}&&StorageKey.All(c=>c is >= '!' and <= '~'));
}
