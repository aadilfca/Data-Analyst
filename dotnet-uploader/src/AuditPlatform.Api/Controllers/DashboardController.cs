using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Dashboard;
using Microsoft.AspNetCore.Mvc;

namespace AuditPlatform.Api.Controllers;

[ApiController]
[Route("api/dashboard")]
public sealed class DashboardController(DashboardService service):ControllerBase
{
    [HttpGet("stats")] public Task<IActionResult> Stats(CancellationToken ct)=>Read("stats",ct);
    [HttpGet("kpi-stats")] public Task<IActionResult> Kpis(CancellationToken ct)=>Read("kpi-stats",ct);
    [HttpGet("db-size")] public Task<IActionResult> Size(CancellationToken ct)=>Read("db-size",ct);
    [HttpGet("overdue-uploads")] public Task<IActionResult> Overdue(CancellationToken ct)=>Read("overdue-uploads",ct);
    private async Task<IActionResult> Read(string operation,CancellationToken ct)
    {Response.Headers.CacheControl="no-store";var result=await service.ReadAsync(HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession,operation,ct);
        return StatusCode(result.Status,result.Value);}
}
