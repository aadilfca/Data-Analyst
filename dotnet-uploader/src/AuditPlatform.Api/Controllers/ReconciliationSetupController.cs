using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Reconciliation;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/reconciliation")]
public sealed class ReconciliationSetupController(ReconciliationSetupService service,
    IOptions<ReconciliationSetupOptions> options) : ControllerBase
{
    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;

    private IActionResult? Guard()
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled || !options.Value.Valid()) return StatusCode(503, new { detail = "Reconciliation setup is unavailable" });
        if (Session is null) return Unauthorized(new { detail = "Not authenticated" });
        return ModelState.IsValid ? null : BadRequest(new { detail = "Invalid reconciliation setup request" });
    }

    [HttpGet("groups")]
    public async Task<IActionResult> Groups([FromQuery] int client_id, CancellationToken ct)
        => Guard() ?? Result(await service.ListGroupsAsync(Session, client_id, ct));

    [HttpGet("account-suggestions")]
    public async Task<IActionResult> AccountSuggestions([FromQuery] int client_id,
        [FromQuery] string? group_label, CancellationToken ct)
        => Guard() ?? Result(await service.AccountSuggestionsAsync(Session, client_id, group_label, ct));

    [HttpPut("groups/{client_id:int}")]
    [RequestSizeLimit(8388608)]
    public async Task<IActionResult> SaveGroup(int client_id, [FromBody] AccountGroupUpdateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.SaveGroupAsync(Session, client_id, request, ct));

    [HttpGet("balances")]
    public async Task<IActionResult> Balances([FromQuery] int client_id,
        [FromQuery] string? group_label, CancellationToken ct)
        => Guard() ?? Result(await service.ListBalancesAsync(Session, client_id, group_label, ct));

    [HttpPut("balances/{client_id:int}")]
    [RequestSizeLimit(8388608)]
    public async Task<IActionResult> ReplaceBalances(int client_id,
        [FromBody] ReconciliationBalanceInputsUpdateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.ReplaceBalancesAsync(Session, client_id, request, ct));

    [HttpGet("category-sets")]
    public async Task<IActionResult> CategorySets([FromQuery] int client_id, CancellationToken ct)
        => Guard() ?? Result(await service.ListCategorySetsAsync(Session, client_id, ct));

    [HttpPost("category-sets")]
    [RequestSizeLimit(1048576)]
    public async Task<IActionResult> CreateCategorySet([FromQuery] int client_id,
        [FromBody] ReportCategorySetCreateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.CreateCategorySetAsync(Session, client_id, request, ct));

    [HttpGet("category-sets/{category_set_id:int}/categories")]
    public async Task<IActionResult> Categories(int category_set_id, CancellationToken ct)
        => Guard() ?? Result(await service.ListCategoriesAsync(Session, category_set_id, ct));

    [HttpPut("category-sets/{category_set_id:int}/categories")]
    [RequestSizeLimit(8388608)]
    public async Task<IActionResult> ReplaceCategories(int category_set_id,
        [FromBody] ReportCategoryDefinitionsUpdateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.ReplaceCategoriesAsync(Session, category_set_id, request, ct));

    [HttpGet("category-sets/{category_set_id:int}/assignments")]
    public async Task<IActionResult> Assignments(int category_set_id, CancellationToken ct)
        => Guard() ?? Result(await service.ListAssignmentsAsync(Session, category_set_id, ct));

    [HttpPut("category-sets/{category_set_id:int}/assignments")]
    [RequestSizeLimit(8388608)]
    public async Task<IActionResult> ReplaceAssignments(int category_set_id,
        [FromBody] ReportCategoryAssignmentsUpdateRequest request, CancellationToken ct)
        => Guard() ?? Result(await service.ReplaceAssignmentsAsync(Session, category_set_id, request, ct));

    private IActionResult Result(ReconciliationSetupResult result)
    {
        if (result.Status == "success") return Ok(result.Value);
        int status = result.Status switch
        {
            "unauthenticated" => 401,
            "forbidden" => 403,
            "not_found" => 404,
            "conflict" or "review" => 409,
            "too_large" => 413,
            "busy" => 429,
            "timeout" => 504,
            "disabled" or "unavailable" => 503,
            _ => 400
        };
        return StatusCode(status, new { detail = result.Value ?? System.Text.Json.JsonSerializer.SerializeToElement(result.Code ?? result.Status), status = result.Status });
    }
}
