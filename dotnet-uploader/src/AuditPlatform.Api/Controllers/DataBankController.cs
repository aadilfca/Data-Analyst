using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.DataBank;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/data")]
public sealed class DataBankController(DataBankService service, IOptions<DataBankOptions> options) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    [HttpGet("availability")]
    public Task<IActionResult> Availability([FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end,
        [FromQuery] int? template_id = null, [FromQuery] bool include_metrics = true, CancellationToken ct = default) =>
        Read(new("availability", client_id, template_id, date_start, date_end, Metrics: include_metrics), ct);
    [HttpGet("availability/metrics")]
    public Task<IActionResult> Metrics([FromQuery] int client_id, CancellationToken ct) => Read(new("metrics", client_id), ct);
    [HttpGet("availability/calendar")]
    public Task<IActionResult> Calendar([FromQuery] int client_id, [FromQuery] int[] template_ids, [FromQuery] int year, [FromQuery] int month, CancellationToken ct) =>
        Read(new("calendar", client_id, Year: year, Month: month, TemplateIds: template_ids), ct);
    [HttpGet("overdue-uploads")]
    public Task<IActionResult> Overdue([FromQuery] int client_id, CancellationToken ct) => Read(new("overdue", client_id), ct);
    [HttpGet("ledger-accounts")]
    public Task<IActionResult> Accounts([FromQuery] int client_id, [FromQuery] int template_id, CancellationToken ct) => Read(new("accounts", client_id, template_id), ct);
    [HttpGet("preview")]
    public Task<IActionResult> Preview([FromQuery] int client_id, [FromQuery] int template_id, [FromQuery] string date_start, [FromQuery] string date_end,
        [FromQuery] int page = 1, [FromQuery] int limit = 50, [FromQuery] string? search = null, [FromQuery] double? amount_min = null,
        [FromQuery] double? amount_max = null, [FromQuery] bool sunday_only = false, [FromQuery] string? data_quality = null,
        [FromQuery] string? ledger_account_code = null, [FromQuery] bool include_metrics = false, CancellationToken ct = default) =>
        Read(new("preview", client_id, template_id, date_start, date_end, page, limit, search, amount_min, amount_max, sunday_only,
            data_quality, ledger_account_code, include_metrics), ct);

    // Native hosts must never forward an unported read or mutation to Python.
    [Route("{**remaining}")]
    public IActionResult Unported() => Guard() ?? StatusCode(503, new { detail = "This Data Bank action is awaiting migration." });
    private IActionResult? Guard()
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Data Bank reads are unavailable" });
        return Session is null ? Unauthorized(new { detail = "Not authenticated" }) : null;
    }
    private async Task<IActionResult> Read(DataBankQuery query, CancellationToken ct)
    {
        if (Guard() is {} error) return error;
        if (!ModelState.IsValid) return BadRequest(new { detail = "Invalid Data Bank request" });
        if (Request.Query.ContainsKey("document_id")) return StatusCode(409, new { detail = "Verification field-policy previews are awaiting migration." });
        var result = await service.ReadAsync(Session, query, ct);
        Response.Headers["X-DataBank-Client"] = query.ClientId.ToString(System.Globalization.CultureInfo.InvariantCulture);
        return result.Status == "success" ? Ok(result.Value) : StatusCode(result.Status switch
        {
            "unauthenticated" => 401, "forbidden" => 403, "not_found" => 404, "review" => 409,
            "too_large" => 413, "busy" => 429, "timeout" => 504, "unavailable" => 503, _ => 400
        }, new { detail = result.Code });
    }
}
