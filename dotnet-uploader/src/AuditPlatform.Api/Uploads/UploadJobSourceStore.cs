using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Uploads;

// Private Supabase Storage REST boundary. Object identity is generated from the
// registered job, never a caller URL, filename, signed URL or public download.
public sealed class UploadJobSourceStore(IOptions<UploadJobOptions> options,HttpClient client)
{
    private string Root=>options.Value.StorageUrl.TrimEnd('/')+"/storage/v1/";
    private void Target(string hash)
    {if(UploadJobStore.StorageIdentity(options.Value)!=hash) throw new UploadJobRejected("job_storage_configuration_changed");}
    private string Object(Guid id)=>options.Value.StorageBucket+"/upload-jobs/"+id.ToString("N")+"/source.bin";
    private HttpRequestMessage Request(HttpMethod method,string path)
    {
        if(!options.Value.Enabled||!options.Value.ValidStorage()) throw new UploadJobRejected("job_storage_configuration");
        var request=new HttpRequestMessage(method,Root+path);
        request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",options.Value.StorageKey);
        request.Headers.Add("apikey",options.Value.StorageKey);return request;
    }
    private static void Require(HttpResponseMessage response)
    {
        if(response.IsSuccessStatusCode) return;
        if(response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden or HttpStatusCode.BadRequest)
            throw new UploadJobRejected("job_storage_access_or_configuration");
        throw new HttpRequestException("Private job storage unavailable",null,response.StatusCode);
    }
    private async Task PrivateAsync(CancellationToken ct)
    {
        using var request=Request(HttpMethod.Get,"bucket/"+options.Value.StorageBucket);
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);Require(response);
        await using var stream=await response.Content.ReadAsStreamAsync(ct);using var buffer=new MemoryStream();var block=new byte[1024];int count;
        while((count=await stream.ReadAsync(block,ct))>0)
        {if(buffer.Length+count>16384) throw new UploadJobRejected("job_storage_configuration");await buffer.WriteAsync(block.AsMemory(0,count),ct);}
        using var json=JsonDocument.Parse(buffer.ToArray());
        if(!json.RootElement.TryGetProperty("public",out var value)||value.ValueKind!=JsonValueKind.False)
            throw new UploadJobRejected("job_storage_must_be_private");
    }
    private async Task<bool> Download(UploadJobData job,Stream? destination,CancellationToken ct)
    {
        using var request=Request(HttpMethod.Get,"object/authenticated/"+Object(job.Id));
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);
        if(response.StatusCode==HttpStatusCode.NotFound) return false;Require(response);
        if(response.Content.Headers.ContentLength is {} advertised&&advertised!=job.Bytes) throw new UploadJobRejected("job_storage_source_changed");
        await using var input=await response.Content.ReadAsStreamAsync(ct);using var digest=IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        byte[] buffer=new byte[65536];long bytes=0;int count;
        while((count=await input.ReadAsync(buffer,ct))>0)
        {
            bytes+=count;if(bytes>job.Bytes||bytes>options.Value.MaximumFileBytes) throw new UploadJobRejected("job_storage_source_changed");
            digest.AppendData(buffer,0,count);if(destination is not null) await destination.WriteAsync(buffer.AsMemory(0,count),ct);
        }
        if(bytes!=job.Bytes||Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant()!=job.SourceHash) throw new UploadJobRejected("job_storage_source_changed");
        return true;
    }
    internal async Task EnsureStoredAsync(UploadJobData job,string path,CancellationToken ct)
    {
        Target(job.StorageHash);
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TransferTimeoutSeconds));ct=deadline.Token;
        await PrivateAsync(ct);if(await Download(job,null,ct)) return;
        await using var file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.Asynchronous);
        if(file.Length!=job.Bytes) throw new UploadJobRejected("job_source_changed");
        using var request=Request(HttpMethod.Post,"object/"+Object(job.Id));request.Headers.Add("x-upsert","false");
        request.Content=new StreamContent(file);request.Content.Headers.ContentType=new("application/octet-stream");request.Content.Headers.ContentLength=job.Bytes;
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);
        // An existing immutable key, or a dropped earlier upload response, is
        // accepted only after full read-back proof. Never overwrite an object.
        if(!response.IsSuccessStatusCode&&response.StatusCode is not (HttpStatusCode.Conflict or HttpStatusCode.BadRequest)) Require(response);
        if(!await Download(job,null,ct)) throw new HttpRequestException("Private job source not yet available");
    }
    internal async Task<bool> RestoreAsync(UploadJobData job,string target,CancellationToken ct)
    {
        Target(job.StorageHash);
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TransferTimeoutSeconds));ct=deadline.Token;
        await PrivateAsync(ct);
        // Caller supplies an owned, generated, CreateNew staging path.
        await using var output=new FileStream(target,FileMode.CreateNew,FileAccess.Write,FileShare.None,65536,FileOptions.Asynchronous);
        if(!await Download(job,output,ct)) return false;await output.FlushAsync(ct);return true;
    }
    internal async Task DeleteAsync(UploadJobCleanup item,CancellationToken ct)
    {
        Target(item.StorageHash);
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(options.Value.TransferTimeoutSeconds));
        await PrivateAsync(deadline.Token);
        using var request=Request(HttpMethod.Delete,"object/"+Object(item.Id));
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,deadline.Token);
        if(response.StatusCode!=HttpStatusCode.NotFound) Require(response);
    }
}
