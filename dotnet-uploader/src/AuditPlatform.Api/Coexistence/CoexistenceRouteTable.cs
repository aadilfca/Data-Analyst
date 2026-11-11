using System.Collections.Frozen;

namespace AuditPlatform.Api.Coexistence;

public enum CoexistenceOwner { DotNet, Legacy }
public enum CoexistenceAccess { Public, Authenticated, Administrator, Client }
public enum CoexistenceAccessDecision { Unavailable, Allowed, Unauthenticated, Forbidden }

// Entries are server registrations, never request/configuration-supplied URLs.
public sealed record CoexistenceRoute(string Id, string Method, string Path,
    CoexistenceOwner Owner, CoexistenceAccess Access);

public interface ICoexistenceAccessGate
{
    ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context,
        CoexistenceRoute route, CancellationToken cancellationToken);
}

public sealed class PendingCoexistenceAccessGate : ICoexistenceAccessGate
{
    // P07 replaces this with proven session/role/client/CSRF checks. Even public
    // bridge registrations cannot be activated by merely changing settings.
    public ValueTask<CoexistenceAccessDecision> AuthorizeAsync(HttpContext context,
        CoexistenceRoute route, CancellationToken cancellationToken) =>
        ValueTask.FromResult(CoexistenceAccessDecision.Unavailable);
}

public sealed class CoexistenceRouteTable
{
    private readonly FrozenDictionary<(string Method, string Path), CoexistenceRoute> routes;
    private readonly FrozenDictionary<string, string> allowedMethods;

    public CoexistenceRouteTable(IEnumerable<CoexistenceRoute> registrations)
    {
        var items = registrations.ToArray();
        if (items.Any(r => !ValidId(r.Id) || !ValidPath(r.Path)
                || !Methods.Contains(r.Method) || !Enum.IsDefined(r.Owner) || !Enum.IsDefined(r.Access))
            || items.Select(r => r.Id).Distinct(StringComparer.Ordinal).Count() != items.Length
            || items.Select(r => (r.Method, Key(r.Path))).Distinct().Count() != items.Length)
            throw new ArgumentException("Coexistence route registrations are invalid or ambiguous.");
        routes = items.ToFrozenDictionary(r => (r.Method, Key(r.Path)));
        allowedMethods = items.GroupBy(r => Key(r.Path), StringComparer.Ordinal)
            .ToFrozenDictionary(g => g.Key, g => string.Join(", ", g.Select(r => r.Method).Order()), StringComparer.Ordinal);
    }

    public CoexistenceRoute? Find(string method, string path) => routes.GetValueOrDefault((method, Key(path)));
    public string? AllowedMethods(string path) => allowedMethods.GetValueOrDefault(Key(path));
    private static string Key(string path) => path.TrimEnd('/').ToLowerInvariant();

    public static bool ValidPath(string? path) => path is not null && path.StartsWith('/')
        && path.Length <= 256 && path.All(c => char.IsAsciiLetterOrDigit(c) || c is '/' or '-' or '_' or '.')
        && !path.Contains("//", StringComparison.Ordinal)
        && !path.Split('/').Any(s => s is "." or "..")
        && Key(path) is not "/healthz" and not "/health/ready";

    private static bool ValidId(string? id) => id is { Length: > 0 and <= 64 }
        && id.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_');
    private static readonly string[] Methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
}
