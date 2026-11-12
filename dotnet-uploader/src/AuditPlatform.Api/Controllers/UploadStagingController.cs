using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Uploads;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

public sealed class UploadStagingController(UploadStagingService staging,IOptions<UploadStagingOptions> options) : ControllerBase
{
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpPost("/api/import-excel")]
    [RequestSizeLimit(268500992)]
    [RequestFormLimits(MultipartBodyLengthLimit=268435456)]
    public async Task<IActionResult> Receive([FromForm] IFormFile? file,[FromQuery(Name="client_id")]int client,
        [FromQuery(Name="template_id")]int template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        if(!ModelState.IsValid || client<=0 || template<=0 || file is null || file.Length<=0 || file.Length>options.Value.MaximumFileBytes
            || Request.Headers["X-Upload-Request"].Count!=1 || !Guid.TryParseExact(Request.Headers["X-Upload-Request"],"D",out var request))
            return BadRequest(new {detail=new {code="invalid_upload",message="Choose a supported workbook within the file-size limit."}});
        await using var stream=file.OpenReadStream();
        return Wire(await staging.ReceiveAsync(Session,client,template,request,file.FileName,stream,ct));
    }
    [HttpGet("/api/upload/staging/{request:guid}")]
    public async Task<IActionResult> Status(Guid request,[FromQuery(Name="client_id")]int client,[FromQuery(Name="template_id")]int template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        return Wire(await staging.StatusAsync(Session,client,template,request,null,false,ct));
    }
    [HttpGet("/api/upload/file/{id:int}")]
    public async Task<IActionResult> FileStatus(int id,[FromQuery(Name="client_id")]int client,[FromQuery(Name="template_id")]int template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        return Wire(await staging.StatusAsync(Session,client,template,null,id,false,ct));
    }
    [HttpDelete("/api/upload/staging/{request:guid}")]
    public async Task<IActionResult> Cleanup(Guid request,[FromQuery(Name="client_id")]int client,[FromQuery(Name="template_id")]int template,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        return Wire(await staging.StatusAsync(Session,client,template,request,null,true,ct));
    }
    [HttpGet("/api/upload/file/{id:int}/headers")]
    public async Task<IActionResult> Headers(int id,[FromQuery(Name="client_id")]int client,[FromQuery(Name="template_id")]int template,[FromQuery]string? sheet,CancellationToken ct)
    {
        if(Guard() is {} error) return error;
        var result=await staging.StatusAsync(Session,client,template,null,id,false,ct);
        if(result.Status!="success") return Wire(result);
        var selected=result.Sheets!.SingleOrDefault(s=>s.Name==sheet);
        return selected is null ? BadRequest(new {detail=new {code="invalid_sheet",message="Choose an existing sheet."}})
            : Ok(new {file_id=id,sheet=selected.Name,headers=selected.Headers});
    }
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled) return StatusCode(503,new {detail="Upload staging is unavailable"});
        if(Session is null) return Unauthorized(new {detail="Not authenticated"});
        if(!Session.TenantContextRequired || Request.Headers["X-App-Context"].Count!=1
            || Request.Headers["X-App-Context"].ToString()!=TenantSessionService.ContextKey(Session))
        { Response.Headers["X-Context-Changed"]="true"; return Conflict(new {detail="Select the workspace again."}); }
        return null;
    }
    private IActionResult Wire(StagingResult r)
    {
        if(r.Status=="success") return Ok(new {file_id=r.FileId,request_id=r.RequestId,state=r.State,byte_count=r.Bytes,sha256=r.Sha256,sheets=r.Sheets});
        return StatusCode(r.Status switch {"unavailable" or "busy"=>503,"forbidden"=>403,"not_found"=>404,"gone"=>410,"conflict"=>409,_=>400},
            new {file_id=r.FileId,request_id=r.RequestId,state=r.State,detail=new {code=r.Code??r.Status,
                message=r.Status=="gone"?"The staged workbook is unavailable. Select the file again.":"The upload could not complete. Check its status before trying again."}});
    }
}
