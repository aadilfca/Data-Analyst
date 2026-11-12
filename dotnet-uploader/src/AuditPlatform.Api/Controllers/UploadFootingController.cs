using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

public sealed class UploadFootingController(UploadFootingService footing,IOptions<UploadFootingOptions> options) : ControllerBase
{
    [HttpPost("/api/upload/file/{id:int}/footing")]
    [RequestSizeLimit(1048576)]
    [PreparationRequestLimit]
    public async Task<IActionResult> Check(int id,[FromBody]FootingRequest? input,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail=new {code="footing_unavailable"}});
        var session=HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
        if(session is null) return Unauthorized(new {detail="Not authenticated"});
        if(!session.TenantContextRequired || Request.Headers["X-App-Context"].Count!=1
            || Request.Headers["X-App-Context"].ToString()!=TenantSessionService.ContextKey(session))
        {Response.Headers["X-Context-Changed"]="true";return Conflict(new {detail="Select the workspace again."});}
        if(!ModelState.IsValid||input?.Validation?.Source is null) return BadRequest(new {detail=new {code="invalid_footing_request"}});
        var result=await footing.CheckAsync(session,id,input,ct);
        if(result.Status=="success") return Ok(result.Output);
        return StatusCode(result.Status switch {"unavailable" or "busy"=>503,"forbidden"=>403,"not_found"=>404,
            "conflict" or "review"=>409,"gone"=>410,"timeout"=>408,_=>400},new {result=result.Output,
                detail=new {code=result.Code??result.Status,message="Review the current workbook and Footing choices, then check again."}});
    }
}
