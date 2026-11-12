using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

// The existing /api/upload/file surface already participates in session and
// CSRF middleware. No legacy request is forwarded or replayed by this endpoint.
public sealed class UploadPreparationController(UploadPreparationService preparation,IOptions<UploadPreparationOptions> options) : ControllerBase
{
    [HttpPost("/api/upload/file/{id:int}/prepare")]
    [RequestSizeLimit(1048576)]
    [PreparationRequestLimit]
    public async Task<IActionResult> Prepare(int id,[FromBody]PreparationRequest? input,CancellationToken ct)
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail=new {code="preparation_unavailable"}});
        var session=HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
        if(session is null) return Unauthorized(new {detail="Not authenticated"});
        if(!session.TenantContextRequired || Request.Headers["X-App-Context"].Count!=1
            || Request.Headers["X-App-Context"].ToString()!=TenantSessionService.ContextKey(session))
        {Response.Headers["X-Context-Changed"]="true";return Conflict(new {detail="Select the workspace again."});}
        if(!ModelState.IsValid || input is null) return BadRequest(new {detail=new {code="invalid_preparation"}});
        var result=await preparation.PrepareAsync(session,id,input,ct);
        if(result.Status=="success") return Ok(result.Output);
        return StatusCode(result.Status switch {"unavailable" or "busy"=>503,"forbidden"=>403,"not_found"=>404,
            "conflict" or "review"=>409,"gone"=>410,"timeout"=>408,_=>400},
            new {request_id=input.RequestId,detail=new {code=result.Code??result.Status,
                message="Review the current workbook, workspace and mapping, then prepare again."}});
    }
}

// Read at most one bounded JSON request before MVC binding. This also covers
// chunked bodies without Content-Length and handles Kestrel's size rejection
// here, before the generic application exception handler can turn it into 500.
public sealed class PreparationRequestLimitAttribute : Attribute,IAsyncResourceFilter
{
    private const int Maximum=1048576;
    public async Task OnResourceExecutionAsync(ResourceExecutingContext context,ResourceExecutionDelegate next)
    {
        var request=context.HttpContext.Request;
        void Reject()
        {
            // The rejected body may remain unread. Do not advertise HTTP/1
            // connection reuse and race Kestrel's bounded drain/close behavior.
            if(request.Protocol.StartsWith("HTTP/1.",StringComparison.Ordinal)) context.HttpContext.Response.Headers.Connection="close";
            context.Result=new StatusCodeResult(413);
        }
        if(request.ContentLength>Maximum) {Reject();return;}
        using var body=new MemoryStream(); var buffer=new byte[65536]; int count;
        try
        {
            while((count=await request.Body.ReadAsync(buffer,context.HttpContext.RequestAborted))>0)
            {
                if(body.Length+count>Maximum) {Reject();return;}
                await body.WriteAsync(buffer.AsMemory(0,count),context.HttpContext.RequestAborted);
            }
        }
        catch(BadHttpRequestException error) when(error.StatusCode==413)
        {Reject();return;}
        body.Position=0; var original=request.Body; request.Body=body;
        try {await next();} finally {request.Body=original;}
    }
}
