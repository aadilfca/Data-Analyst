using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.DataBank;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/data")]
public sealed class DataBankController(DataBankService service, IOptions<DataBankOptions> options, DataBankExportService exports) : ControllerBase
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
    public Task<IActionResult> Accounts([FromQuery] int client_id, [FromQuery] int template_id, [FromQuery] bool history = false, CancellationToken ct = default) => Read(new(history ? "history-accounts" : "accounts", client_id, template_id), ct);
    [HttpGet("history")]
    public Task<IActionResult> History([FromQuery] int client_id, [FromQuery] int template_id, [FromQuery] int page = 1, [FromQuery] int limit = 25,
        [FromQuery] string? action = "all", [FromQuery] string? ledger_account_code = null, CancellationToken ct = default) =>
        Read(new("history", client_id, template_id, Page: page, Limit: limit, Account: ledger_account_code, Action: (action ?? "all").Trim().ToLowerInvariant()), ct);
    [HttpGet("rows/{id:int}/history")]
    public Task<IActionResult> Revisions(int id, [FromQuery] int client_id, [FromQuery] int template_id, [FromQuery] string source = "uploads",
        [FromQuery] int page = 1, [FromQuery] int limit = 25, CancellationToken ct = default) =>
        Read(new("revisions", client_id, template_id, Page: page, Limit: limit, RowId: id, RevisionSource: source), ct);
    [HttpGet("template/{id:int}/export")]
    public Task<IActionResult> Export(int id, [FromQuery] int client_id, [FromQuery] string date_start, [FromQuery] string date_end,
        [FromQuery] string? search = null, [FromQuery] double? amount_min = null, [FromQuery] double? amount_max = null,
        [FromQuery] bool sunday_only = false, [FromQuery] string? data_quality = null, [FromQuery] string? ledger_account_code = null, CancellationToken ct = default) =>
        Download(new("preview", client_id, id, date_start, date_end, Search: search, Minimum: amount_min, Maximum: amount_max, Sunday: sunday_only, Quality: data_quality, Account: ledger_account_code), false, ct);
    [HttpGet("template/{id:int}/backup")]
    public Task<IActionResult> Backup(int id, [FromQuery] int client_id, CancellationToken ct, [FromQuery] bool purge = false) => Download(new("backup", client_id, id), true, ct, purge);
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
    private async Task<IActionResult> Download(DataBankQuery query, bool backup, CancellationToken ct, bool purge = false)
    {
        if (Guard() is {} error) return error;
        if (!ModelState.IsValid || Request.Query.ContainsKey("document_id")) return BadRequest(new { detail = "Invalid export scope" });
        var result = await exports.DownloadAsync(Session, query, backup, ct, purge);
        Response.Headers["X-DataBank-Client"] = query.ClientId.ToString(System.Globalization.CultureInfo.InvariantCulture);
        Response.Headers["X-DataBank-Template"] = query.TemplateId.ToString(); Response.Headers["X-Export-Scope"] = backup ? "backup" : "matching";
        if(result.PurgeProof is not null) Response.Headers["X-Purge-Backup-Proof"]=result.PurgeProof;
        if (result.Status == "success") return File(result.Bytes!, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", result.Name);
        return StatusCode(result.Status switch { "unauthenticated" => 401, "forbidden" => 403, "not_found" => 404, "review" => 409,
            "too_large" => 413, "busy" => 429, "timeout" => 504, "unavailable" => 503, _ => 400 }, new { code = result.Code, detail = result.Status switch {
                "too_large" => "The complete workbook could not be created within the size or Excel cell limits. Try a smaller matching-row export; a full backup needs review of those limits.",
                "busy" => "Another download is being prepared. Retry when it finishes.", "timeout" => "The download timed out. Request it again to retry.",
                "forbidden" => "This download is not available for your current access.", "unavailable" => "Native Data Bank downloads are disabled.",
                "review" => "Stored data needs review before this download can be prepared.", _ => "The download could not be prepared for this selection." } });
    }
}
