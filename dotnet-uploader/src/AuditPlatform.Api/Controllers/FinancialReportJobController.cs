using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Reports;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/reconciliation/financial-reports/jobs")]
public sealed class FinancialReportJobController(FinancialReportJobService jobs,
    IOptions<FinancialReportJobOptions> options):ControllerBase
{
    private VerifiedSession? Session=>HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl="no-store";
        if(!options.Value.Enabled)return StatusCode(503,new {detail="Financial report jobs are unavailable"});
        if(Session is null)return Unauthorized(new {detail="Not authenticated"});
        return null;
    }

    [HttpPost]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> Submit([FromBody]FinancialReportJobRequest request,CancellationToken ct)
    {
        if(Guard() is{} error)return error;
        return Result(await jobs.SubmitAsync(Session,request,ct),accepted:true);
    }

    [HttpGet]
    public async Task<IActionResult> Get([FromQuery]Guid id,CancellationToken ct)
    {
        if(Guard() is{} error)return error;
        return Result(await jobs.GetAsync(Session,id,ct));
    }

    [HttpPost("cancel")]
    [RequestSizeLimit(1024)]
    public async Task<IActionResult> Cancel([FromQuery]Guid id,CancellationToken ct)
    {
        if(Guard() is{} error)return error;
        return Result(await jobs.CancelAsync(Session,id,ct));
    }

    [HttpGet("download")]
    public async Task<IActionResult> Download([FromQuery]Guid id,CancellationToken ct)
    {
        if(Guard() is{} error)return error;
        var result=await jobs.DownloadAsync(Session,id,ct);
        return result is null?NotFound(new {detail="Report job artifact is unavailable"})
            :File(result.Bytes,result.ContentType,result.FileName);
    }

    private IActionResult Result(FinancialReportJobResult result,bool accepted=false)=>result.Status switch
    {
        "success"=>accepted?AcceptedAtAction(nameof(Get),new {id=result.Job!.Id},result.Job):Ok(result.Job),
        "invalid"=>BadRequest(new {detail=result.Code}),
        "unauthenticated"=>Unauthorized(new {detail="Not authenticated"}),
        "forbidden"=>Forbid(),
        "not_found"=>NotFound(new {detail=result.Code}),
        "unavailable"=>StatusCode(503,new {detail=result.Code}),
        "conflict"=>Conflict(new {detail=result.Code}),
        _=>StatusCode(503,new {detail="Financial report job request could not be completed"})
    };
}
