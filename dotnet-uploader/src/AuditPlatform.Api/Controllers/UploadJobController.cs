using System.Text.Json;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/upload/file/{file:int}/jobs")]
public sealed class UploadJobController(UploadJobService jobs,PostgresSessionStore sessions,IOptions<UploadJobOptions> options):ControllerBase
{
    private static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web)
    {UnmappedMemberHandling=JsonUnmappedMemberHandling.Disallow,NumberHandling=JsonNumberHandling.Strict,MaxDepth=32};
    private static readonly SemaphoreSlim Bodies=new(2);
    private async Task<VerifiedSession?> Actor(CancellationToken ct)=>!Request.Cookies.TryGetValue("session_token",out var token)?null:await sessions.FindAsync(token,ct);
    private bool Context(VerifiedSession? s)=>s is null||Request.Headers["X-App-Context"].ToString()==TenantSessionService.ContextKey(s);
    private IActionResult Result(UploadJobResult result)
    {
        Response.Headers.CacheControl="no-store";
        int status=result.Status switch {"success"=>200,"unavailable"=>503,"unauthenticated"=>401,"forbidden"=>403,"not_found"=>404,"invalid"=>400,"busy"=>409,_=>409};
        return new JsonResult(result){StatusCode=status};
    }
    [HttpPost]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Submit(int file,CancellationToken ct)
    {
        if(!options.Value.Enabled) return Result(new("unavailable"));
        var actor=await Actor(ct);if(actor is null) return Result(new("unauthenticated"));if(!Context(actor)) return Result(new("conflict",Code:"stale_application_context"));
        if(!await Bodies.WaitAsync(0,ct)) return Result(new("busy",Code:"job_request_busy"));
        using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(30));var bodyToken=deadline.Token;
        try
        {
            using var buffer=new MemoryStream();byte[] block=new byte[16384];int count;
            while((count=await Request.Body.ReadAsync(block,bodyToken))>0)
            {if(buffer.Length+count>1048576) {Response.Headers.CacheControl="no-store";return StatusCode(413);}await buffer.WriteAsync(block.AsMemory(0,count),bodyToken);}
            using(var parsed=JsonDocument.Parse(buffer.ToArray(),new JsonDocumentOptions{MaxDepth=32})) RequireUnique(parsed.RootElement);
            var input=JsonSerializer.Deserialize<UploadJobRequest>(buffer.ToArray(),Json);
            if(input is null) return Result(new("invalid",Code:"invalid_job_request"));
            return Result(await jobs.SubmitAsync(actor,file,input,ct));
        }
        catch(JsonException) {return Result(new("invalid",Code:"invalid_job_request"));}
        catch(OperationCanceledException) when(!ct.IsCancellationRequested) {return Result(new("timeout",Code:"job_request_timed_out"));}
        catch(BadHttpRequestException e) when(e.StatusCode==413) {Response.Headers.CacheControl="no-store";return StatusCode(413);}
        finally {Bodies.Release();}
    }
    private static void RequireUnique(JsonElement element)
    {
        if(element.ValueKind==JsonValueKind.Object)
        {
            var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(var p in element.EnumerateObject()) {if(!names.Add(p.Name)) throw new JsonException();RequireUnique(p.Value);}
        }
        else if(element.ValueKind==JsonValueKind.Array) foreach(var child in element.EnumerateArray()) RequireUnique(child);
    }
    [HttpGet("{id:guid}")]
    public Task<IActionResult> Get(int file,Guid id,CancellationToken ct)=>Action(file,id,"status",ct);
    [HttpPost("{id:guid}/cancel")]
    [RequestSizeLimit(1024)]
    public Task<IActionResult> Cancel(int file,Guid id,CancellationToken ct)=>Action(file,id,"cancel",ct);
    [HttpPost("{id:guid}/resume")]
    [RequestSizeLimit(1024)]
    public Task<IActionResult> Resume(int file,Guid id,CancellationToken ct)=>Action(file,id,"resume",ct);
    private async Task<IActionResult> Action(int file,Guid id,string action,CancellationToken ct)
    {
        if(!options.Value.Enabled) return Result(new("unavailable"));var actor=await Actor(ct);
        if(actor is null) return Result(new("unauthenticated"));
        if(!Context(actor)) return Result(new("conflict",Code:"stale_application_context"));
        if(action!="status")
        {
            using var deadline=CancellationTokenSource.CreateLinkedTokenSource(ct);deadline.CancelAfter(TimeSpan.FromSeconds(30));
            try
            {
                byte[] bytes=new byte[1025];int total=0,count;
                while((count=await Request.Body.ReadAsync(bytes.AsMemory(total),deadline.Token))>0)
                {total+=count;if(total>1024) {Response.Headers.CacheControl="no-store";return StatusCode(413);}}
                if(total>0)
                {using var json=JsonDocument.Parse(bytes.AsMemory(0,total));if(json.RootElement.ValueKind!=JsonValueKind.Object||json.RootElement.EnumerateObject().Any()) return Result(new("invalid",Code:"invalid_job_request"));}
            }
            catch(JsonException) {return Result(new("invalid",Code:"invalid_job_request"));}
            catch(BadHttpRequestException e) when(e.StatusCode==413) {Response.Headers.CacheControl="no-store";return StatusCode(413);}
            catch(OperationCanceledException) when(!ct.IsCancellationRequested) {return Result(new("timeout",Code:"job_request_timed_out"));}
        }
        return Result(action=="status"?await jobs.GetAsync(actor,file,id,ct):action=="cancel"?await jobs.CancelAsync(actor,file,id,ct):await jobs.ResumeAsync(actor,file,id,ct));
    }
}
