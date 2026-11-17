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

    public CoexistenceRoute? Find(string method, string path)
    {
        var key = Key(path);
        if (routes.TryGetValue((method, key), out var exact)) return exact;
        return routes.Values.FirstOrDefault(route => route.Method == method && IsTemplateMatch(route.Path, path));
    }

    public static bool MatchesPath(CoexistenceRoute route, string path) =>
        string.Equals(Key(route.Path), Key(path), StringComparison.Ordinal) || IsTemplateMatch(route.Path, path);

    public string? AllowedMethods(string path)
    {
        if (allowedMethods.TryGetValue(Key(path), out var exact)) return exact;
        var methods = routes.Values.Where(route => IsTemplateMatch(route.Path, path))
            .Select(route => route.Method).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        return methods.Length == 0 ? null : string.Join(", ", methods);
    }
    private static string Key(string path) => path.TrimEnd('/').ToLowerInvariant();

    public static bool ValidPath(string? path) => path is not null && path.StartsWith('/') && path.Length <= 256
        && !path.Contains("//", StringComparison.Ordinal)
        && !path.Split('/').Any(s => s is "." or "..")
        && path.Split('/').All(segment => IsIntegerParameter(segment)
            || segment.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_' or '.'))
        && Key(path) is not "/healthz" and not "/health/ready";

    private static bool IsTemplateMatch(string pattern, string path)
    {
        path = Key(path);
        var expected = pattern.Split('/'); var actual = path.Split('/');
        if (expected.Length != actual.Length) return false;
        for (int i = 0; i < expected.Length; i++)
        {
            if (IsIntegerParameter(expected[i]))
            {
                if (actual[i].Length is 0 or > 10 || actual[i].Any(c => !char.IsAsciiDigit(c))
                    || !int.TryParse(actual[i], out int id) || id <= 0) return false;
            }
            else if (!string.Equals(expected[i], actual[i], StringComparison.OrdinalIgnoreCase)) return false;
        }
        return true;
    }

    private static bool IsIntegerParameter(string segment)
    {
        if (segment.Length is < 7 or > 40 || segment[0] != '{' || !segment.EndsWith(":int}", StringComparison.Ordinal)) return false;
        var name = segment[1..^5];
        return name.Length > 0 && name.All(c => char.IsAsciiLetterOrDigit(c) || c == '_');
    }

    private static bool ValidId(string? id) => id is { Length: > 0 and <= 64 }
        && id.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_');
    private static readonly string[] Methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
}
