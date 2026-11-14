using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.TrialBalance;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/trial-balance")]
public sealed class TrialBalanceController(TrialBalanceReviewService service,IOptions<TrialBalanceReviewOptions> options) : ControllerBase
{
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled)return StatusCode(503,new {detail="Trial Balance review is unavailable"});
        if(Session is null)return Unauthorized(new {detail="Not authenticated"});
        return ModelState.IsValid?null:BadRequest(new {detail="Invalid Trial Balance request"});
    }
    [HttpGet("uploads")]
    public async Task<IActionResult> Uploads([FromQuery]int client_id,CancellationToken ct)
        =>Guard()??Result(await service.ListAsync(Session,client_id,ct));
    [HttpGet("accounts")]
    public async Task<IActionResult> Accounts([FromQuery]int client_id,[FromQuery]int upload_id,[FromQuery]int page=1,
        [FromQuery]int limit=100,[FromQuery]string search="",[FromQuery]bool? is_leaf=null,CancellationToken ct=default)
        =>Guard()??Result(await service.ReadAsync(Session,new(client_id,upload_id,page,limit,search,is_leaf),ct));
    [HttpGet("summary")]
    public async Task<IActionResult> Summary([FromQuery]int client_id,[FromQuery]int upload_id,CancellationToken ct)
    {
        if(Guard() is {} error)return error;var result=await service.ReadAsync(Session,new(client_id,upload_id,Limit:1),ct);
        return result.Status=="success"?Ok(result.Value!.Value.GetProperty("summary")):Result(result);
    }
    [HttpGet("assignment-values")]
    [HttpGet("/api/account-control-center/mapping")]
    [HttpGet("/api/account-control-center/cashflow")]
    public async Task<IActionResult> Assignments([FromQuery]int client_id,[FromQuery]int trial_balance_upload_id,[FromQuery]int page=1,[FromQuery]int limit=100,CancellationToken ct=default)
        =>Guard()??Result(await service.ReadAsync(Session,new(client_id,trial_balance_upload_id,page,limit),ct));
    [HttpPost("uploads/{upload:int}/review/{kind}/preview")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Preview(int upload,string kind,[FromQuery]int client_id,[FromBody]TrialBalanceReviewRequest request,CancellationToken ct)
        =>Guard()??Result(await service.PreviewAsync(Session,client_id,upload,kind,request,ct));
    [HttpPost("uploads/{upload:int}/review/{kind}")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Save(int upload,string kind,[FromQuery]int client_id,[FromBody]TrialBalanceReviewRequest request,CancellationToken ct)
        =>Guard()??Result(await service.SaveAsync(Session,client_id,upload,kind,request,ct));
    [HttpGet("uploads/{upload:int}/review/operations/{operation:guid}")]
    public async Task<IActionResult> Status(int upload,Guid operation,[FromQuery]int client_id,CancellationToken ct)
        =>Guard()??Result(await service.StatusAsync(Session,client_id,upload,operation,ct));
    [Route("{**remaining}")]
    [Route("/api/account-control-center/{**remaining}")]
    public IActionResult Unported()=>Guard()??StatusCode(503,new {detail="This Trial Balance action is unavailable in this workspace."});
    private IActionResult Result(TrialBalanceReviewResult result)
    {
        if(result.Status=="success")return result.Outcome is null?Ok(result.Value):Ok(new {updated=result.Outcome.Updated,upload_id=result.Outcome.UploadId,
            corrected_count=result.Outcome.CorrectedCount,operation_id=result.Outcome.OperationId,outcome=JsonSerializer.SerializeToElement(result.Outcome,new JsonSerializerOptions{PropertyNamingPolicy=JsonNamingPolicy.SnakeCaseLower})});
        return StatusCode(result.Status switch {"unauthenticated"=>401,"forbidden"=>403,"not_found"=>404,"conflict" or "review" or "unknown"=>409,"too_large"=>413,"busy"=>429,"timeout"=>504,"disabled" or "unavailable"=>503,_=>400},new {detail=result.Code,status=result.Status});
    }
}
