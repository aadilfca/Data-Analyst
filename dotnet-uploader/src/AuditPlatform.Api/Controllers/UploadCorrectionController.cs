using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/upload/file")]
public sealed class UploadCorrectionController(UploadCorrectionService corrections,IOptions<UploadCorrectionOptions> options) : ControllerBase
{
    private static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web){NumberHandling=JsonNumberHandling.Strict,MaxDepth=32};
    [HttpPost("{id:int}/corrections")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Check(int id,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {error="unavailable"});
        if(HttpContext.Items[typeof(VerifiedSession)] is not VerifiedSession session) return Unauthorized();
        if(Request.Headers["X-App-Context"].Count!=1||!string.Equals(Request.Headers["X-App-Context"],TenantSessionService.ContextKey(session),StringComparison.Ordinal))
            return Conflict(new {error="context_changed"});
        if(!Request.HasJsonContentType()) return StatusCode(415,new {error="json_required"});
        CorrectionRequest? request;
        // Bound chunked bodies too, and catch Kestrel's size rejection here
        // before the shared error logger converts an uncaught exception to 500.
        try
        {
            if(Request.ContentLength is >1048576) return StatusCode(413,new {error="request_too_large"});
            using var body=new MemoryStream();byte[] chunk=new byte[16384];int count;
            while((count=await Request.Body.ReadAsync(chunk,ct))>0)
            {
                if(body.Length+count>1048576) return StatusCode(413,new {error="request_too_large"});
                body.Write(chunk,0,count);
            }
            body.Position=0;request=await JsonSerializer.DeserializeAsync<CorrectionRequest>(body,Json,ct);
        }
        catch(BadHttpRequestException error) when(error.StatusCode==413) {return StatusCode(413,new {error="request_too_large"});}
        catch(JsonException) {return BadRequest(new {error="invalid_correction_request"});}
        if(request is null) return BadRequest(new {error="invalid_correction_request"});
        var result=await corrections.CheckAsync(session,id,request,ct);
        return result.Status switch {
            "success"=>Ok(result.Output),"unauthenticated"=>Unauthorized(),"forbidden"=>StatusCode(403),"not_found"=>NotFound(),
            "gone" or "expired"=>StatusCode(410,new {error=result.Code??"expired"}),"busy"=>StatusCode(429,new {error="busy"}),
            "timeout"=>StatusCode(408,new {error=result.Code}),"unavailable"=>StatusCode(503,new {error="unavailable"}),
            "conflict"=>Conflict((object?)result.Output??new {error=result.Code}),
            "review"=>Conflict(new {error=result.Code}),_=>BadRequest((object?)result.Output??new {error=result.Code??result.Status})};
    }
}
