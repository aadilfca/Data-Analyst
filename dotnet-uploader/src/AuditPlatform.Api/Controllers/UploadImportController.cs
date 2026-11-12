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
public sealed class UploadImportController(UploadImportService imports,IOptions<UploadImportOptions> options):ControllerBase
{
    [HttpPost("/api/upload/file/{id:int}/atomic-import")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Import(int id,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {code="import_unavailable"});
        if(HttpContext.Items[typeof(VerifiedSession)] is not VerifiedSession session) return Unauthorized();
        if(Request.Headers["X-App-Context"]!=TenantSessionService.ContextKey(session)) return Conflict(new {code="context_changed"});
        UploadImportRequest? input;
        try
        {
            // Count actual bytes as chunked requests may omit Content-Length.
            using var body=new MemoryStream();var buffer=new byte[16384];int count;
            while((count=await Request.Body.ReadAsync(buffer,ct))>0)
            {if(body.Length+count>1048576) return StatusCode(413);await body.WriteAsync(buffer.AsMemory(0,count),ct);}
            input=JsonSerializer.Deserialize<UploadImportRequest>(body.ToArray(),new JsonSerializerOptions(JsonSerializerDefaults.Web){NumberHandling=JsonNumberHandling.Strict,MaxDepth=32});
        }
        catch(BadHttpRequestException error) when(error.StatusCode==413) {return StatusCode(413);}
        catch(JsonException) {return BadRequest(new {code="invalid_import_request"});}
        if(input is null) return BadRequest();
        var result=await imports.ImportAsync(session,id,input,ct);
        return StatusCode(result.Status switch {"success"=>200,"unauthenticated"=>401,"forbidden"=>403,"not_found"=>404,"gone"=>410,"invalid"=>400,
            "conflict" or "review"=>409,"busy"=>429,"timeout"=>504,_=>503},result);
    }
}
