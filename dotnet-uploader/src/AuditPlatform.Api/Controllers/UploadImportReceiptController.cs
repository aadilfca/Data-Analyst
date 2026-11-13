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
public sealed class UploadImportReceiptController(UploadImportReceiptService receipts,IOptions<UploadImportOptions> options):ControllerBase
{
    private IActionResult? Access(out VerifiedSession? session)
    {
        Response.Headers.CacheControl="no-store";session=null;
        if(!options.Value.Enabled) return StatusCode(503,new {code="receipt_unavailable"});
        session=HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;if(session is null) return Unauthorized();
        if(Request.Headers["X-App-Context"]!=TenantSessionService.ContextKey(session)) return Conflict(new {code="context_changed"});return null;
    }
    [HttpGet("/api/upload/file/{file:int}/receipt/{id:guid}")]
    public async Task<IActionResult> Get(int file,Guid id,CancellationToken ct)
    {var blocked=Access(out var actor);if(blocked is not null) return blocked;var result=await receipts.GetAsync(actor,file,id,ct);return StatusCode(Status(result.Status),result);}

    [HttpGet("/api/upload/file/{file:int}/receipt/{id:guid}/workbook")]
    public async Task<IActionResult> Workbook(int file,Guid id,CancellationToken ct)
    {
        var blocked=Access(out var actor);if(blocked is not null) return blocked;var result=await receipts.WorkbookAsync(actor,file,id,ct);
        if(result.Status!="success") return StatusCode(Status(result.Status),new {status=result.Status,code=result.Code});
        return File(result.Bytes!,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",$"upload-receipt-{id:N}.xlsx");
    }
    [HttpPost("/api/upload/file/{file:int}/receipt/check")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Check(int file,CancellationToken ct)
    {
        var blocked=Access(out var actor);if(blocked is not null) return blocked;ValidationPlanRequest? input;
        try
        {
            using var body=new MemoryStream();var buffer=new byte[16384];int read;
            while((read=await Request.Body.ReadAsync(buffer,ct))>0) {if(body.Length+read>1048576) return StatusCode(413);await body.WriteAsync(buffer.AsMemory(0,read),ct);}
            input=JsonSerializer.Deserialize<ValidationPlanRequest>(body.ToArray(),new JsonSerializerOptions(JsonSerializerDefaults.Web){NumberHandling=JsonNumberHandling.Strict,MaxDepth=32});
        }
        catch(BadHttpRequestException error) when(error.StatusCode==413) {return StatusCode(413);}
        catch(JsonException) {return BadRequest(new {code="invalid_receipt_request"});}
        if(input is null) return BadRequest();var result=await receipts.CheckNoChangesAsync(actor,file,input,ct);return StatusCode(Status(result.Status),result);
    }
    private static int Status(string status)=>status switch {"success"=>200,"unavailable"=>503,"unauthenticated"=>401,"forbidden"=>403,"not_found"=>404,"gone"=>410,
        "invalid"=>400,"busy"=>429,"timeout"=>504,_=>409};
}
