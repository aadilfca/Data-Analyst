using System.Globalization;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Clients;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Controllers;

[Route("api/clients")]
public sealed class ClientsController(TenantClientService clients, IOptions<ClientManagementOptions> options) : ControllerBase
{
    public sealed record ClientInput(
        [property: JsonPropertyName("client_code")] string? Code,
        [property: JsonPropertyName("client_name")] string? Name,
        string? Industry, [property: JsonPropertyName("contact_person")] string? ContactPerson,
        string? Email, string? Phone, [property: JsonPropertyName("is_active")] bool IsActive = true);

    [HttpGet]
    public async Task<IActionResult> List([FromQuery(Name="include_inactive")] bool inactive,
        [FromQuery] string? search, [FromQuery] int? limit, [FromQuery] int offset, CancellationToken ct)
    {
        if (Guard() is { } error) return error;
        if (!ModelState.IsValid || limit is < 1 or > 200 || offset < 0) return Invalid();
        var result = await clients.ListAsync(Session, inactive, search, limit, offset, ct);
        if (result.Status != ClientStatus.Success) return Error(result.Status);
        if (limit is not null) Response.Headers["X-Total-Count"] = result.Total.ToString(CultureInfo.InvariantCulture);
        return Ok(result.Clients!.Select(ToWire));
    }

    [HttpGet("{id:int}")]
    public async Task<IActionResult> Get(int id, CancellationToken ct)
    {
        if (Guard() is { } error) return error;
        var result = await clients.GetAsync(Session, id, ct);
        return result.Status == ClientStatus.Success ? Ok(ToWire(result.Clients![0])) : Error(result.Status);
    }

    [HttpPost]
    public Task<IActionResult> Create([FromBody] ClientInput? input, CancellationToken ct) => Save(null, input, ct, create: true);
    [HttpPut("{id:int}")]
    public Task<IActionResult> Update(int id, [FromBody] ClientInput? input, CancellationToken ct) => Save(id, input, ct);

    private async Task<IActionResult> Save(int? id, ClientInput? input, CancellationToken ct, bool create = false)
    {
        if (Guard(create: create, write: !create) is { } error) return error;
        if (!ModelState.IsValid || input?.Code is null || input.Name is null) return Invalid();
        var result = await clients.SaveAsync(Session, id,
            new(input.Code,input.Name,input.Industry,input.ContactPerson,input.Email,input.Phone,input.IsActive), ct);
        return result.Status == ClientStatus.Success
            ? id is null ? Ok(new { message = $"Client '{input.Name}' created successfully", client_id = result.ClientId })
                : Ok(new { message = "Client updated successfully" }) : Error(result.Status);
    }

    [HttpDelete("{id:int}")]
    public Task<IActionResult> Delete(int id, CancellationToken ct) => SetActive(id, false, ct);
    [HttpPost("{id:int}/restore")]
    public Task<IActionResult> Restore(int id, CancellationToken ct) => SetActive(id, true, ct);
    private async Task<IActionResult> SetActive(int id, bool active, CancellationToken ct)
    {
        if (Guard(write: true) is { } error) return error;
        var result = await clients.SetActiveAsync(Session, id, active, ct);
        return result.Status == ClientStatus.Success
            ? Ok(new { message = active ? "Client restored successfully" : "Client deleted (soft-delete) successfully" }) : Error(result.Status);
    }

    private VerifiedSession? Session => HttpContext.Items[typeof(VerifiedSession)] as VerifiedSession;
    private IActionResult? Guard(bool create = false, bool write = false)
    {
        Response.Headers.CacheControl = "no-store";
        if (!options.Value.Enabled) return StatusCode(503, new { detail = "Client management is unavailable" });
        if (create && !options.Value.CreateEnabled) return StatusCode(503, new { detail = "Client creation is disabled" });
        if (write && !options.Value.WriteEnabled) return StatusCode(503, new { detail = "Client administration changes are disabled" });
        return Session is null ? Unauthorized(new { detail = "Not authenticated" }) : null;
    }
    private IActionResult Invalid() => UnprocessableEntity(new { detail = new[] {
        new { type="invalid_request", loc=new[] { "request" }, msg="Invalid client request", input="[redacted]" } } });
    internal static object ToWire(TenantClient c) => new { id=c.Id, client_code=c.Profile.Code,
        client_name=c.Profile.Name, industry=c.Profile.Industry, contact_person=c.Profile.ContactPerson,
        email=c.Profile.Email, phone=c.Profile.Phone, is_active=c.Profile.IsActive, created_at=c.CreatedAt.ToString("O") };
    private IActionResult Error(ClientStatus status) => StatusCode(Status(status), new { detail = Message(status) });
    internal static int Status(ClientStatus status) => status switch
    { ClientStatus.Forbidden => 403, ClientStatus.NotFound => 404, ClientStatus.AmbiguousTenant => 409, ClientStatus.Invalid or ClientStatus.Conflict => 400, _ => 503 };
    internal static string Message(ClientStatus status) => status switch
    { ClientStatus.Forbidden => "Client access denied", ClientStatus.NotFound => "Client not found",
        ClientStatus.AmbiguousTenant => "Tenant selection is required", ClientStatus.Conflict => "Client code unavailable",
        ClientStatus.Invalid => "Invalid client details", _ => "Client management is unavailable" };
}
