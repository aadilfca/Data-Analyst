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
[Route("api/upload/file")]
public sealed class UploadValidationPlanController(UploadValidationPlanService plans,IOptions<UploadValidationPlanOptions> options) : ControllerBase
{
    private static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web){NumberHandling=JsonNumberHandling.Strict,MaxDepth=32};
    [HttpPost("{id:int}/validation-plan")][RequestSizeLimit(1048576)]
    public Task<IActionResult> Create(int id,CancellationToken ct)=>Execute<ValidationPlanRequest>(r=>plans.CreateAsync(Session(),id,r,ct),ct);
    [HttpPost("{id:int}/validation-plan/check")][RequestSizeLimit(1048576)]
    public Task<IActionResult> Check(int id,CancellationToken ct)=>Execute<ValidationPlanCheck>(r=>plans.CheckAsync(Session(),id,r,ct),ct);
    private VerifiedSession? Session()=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    private async Task<IActionResult> Execute<T>(Func<T,Task<ValidationPlanResult>> run,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {error="unavailable"});
        var session=Session();if(session is null) return Unauthorized();
        if(Request.Headers["X-App-Context"].Count!=1||!string.Equals(Request.Headers["X-App-Context"],TenantSessionService.ContextKey(session),StringComparison.Ordinal)) return Conflict(new {error="context_changed"});
        if(!Request.HasJsonContentType()) return StatusCode(415);
        T? request;
        try
        {
            if(Request.ContentLength is >1048576) return StatusCode(413);
            using var body=new MemoryStream();byte[] buffer=new byte[16384];int count;
            while((count=await Request.Body.ReadAsync(buffer,ct))>0)
            {if(body.Length+count>1048576) return StatusCode(413);body.Write(buffer,0,count);}
            body.Position=0;request=await JsonSerializer.DeserializeAsync<T>(body,Json,ct);
        }
        catch(BadHttpRequestException error) when(error.StatusCode==413) {return StatusCode(413);}
        catch(JsonException) {return BadRequest(new {error="invalid_plan_request"});}
        if(request is null) return BadRequest();
        var result=await run(request);
        return result.Status switch {"success"=>Ok(result.Output),"unauthenticated"=>Unauthorized(),"forbidden"=>StatusCode(403),
            "not_found"=>NotFound(),"gone"=>StatusCode(410),"unavailable"=>StatusCode(503),"busy"=>StatusCode(429),"timeout"=>StatusCode(408),
            "conflict" or "review"=>Conflict(new {error=result.Code}),_=>BadRequest(new {error=result.Code??result.Status})};
    }
}
