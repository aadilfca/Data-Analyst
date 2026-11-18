using System.Security.Claims;
using System.Text.Json;

namespace AuditPlatform.Api.Logging;

// These are server-side service inputs, never public request DTOs. P07 owns
// authenticating the principal; a submitted name/header cannot create an actor.
public sealed record LogActor(string Kind, int? UserId, string Name)
{
    public static LogActor Anonymous { get; } = new("anonymous", null, "Anonymous");
    public static LogActor System { get; } = new("system", null, "System");

    public static LogActor FromPrincipal(ClaimsPrincipal principal)
    {
        if (principal.Identity?.IsAuthenticated != true)
            return Anonymous;
        if (!int.TryParse(principal.FindFirstValue(ClaimTypes.NameIdentifier), out var id)
            || id <= 0 || string.IsNullOrWhiteSpace(principal.Identity.Name))
            throw new InvalidOperationException("Authenticated logging requires a verified user ID and name.");
        return new("authenticated", id, principal.Identity.Name);
    }
}

public sealed record AuditEvent(
    Guid EventId, Guid OperationId, LogActor Actor, int? ClientId,
    string Action, string Module, JsonElement? Metadata = null, string? CorrelationId = null);

// ValueFields is an explicit server-owned policy from the module/template port.
// Unapproved/sensitive changed values are redacted; DELETE keeps a hash only.
public sealed record DataChangeAudit(
    AuditEvent Event, int ClientDataId, JsonElement? OldRow, JsonElement? NewRow,
    IReadOnlySet<string> ValueFields);

public sealed record DiagnosticError(
    Guid EventId, string CorrelationId, LogActor Actor, int? ClientId,
    string Module, string Endpoint, string Method, int StatusCode,
    string Category, string SafeMessage, string? SafeTrace, JsonElement? Metadata,
    bool OutcomeUnknown = false, Guid? OperationId = null, int? TenantId = null);

public sealed record DiagnosticLogResult(
    int? ErrorId, string Code, string UserMessage, string SuggestedAction, string Persistence);

// Only server-side authorization/module code may attach this typed context.
// Route/query/body/header values alone never establish authorized log scope.
public sealed record AuthorizedLogScope(int? ClientId, Guid? OperationId);
