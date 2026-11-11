namespace AuditPlatform.Api.Authentication;

public sealed class AuthenticationService(PostgresSessionStore sessions)
{
    public Task<VerifiedSession?> ResolveAsync(HttpContext context, CancellationToken cancellationToken)
    {
        var token = context.Request.Cookies["session_token"];
        if (!ValidToken(token))
            return Task.FromResult<VerifiedSession?>(null);
        return sessions.FindAsync(token!, cancellationToken);
    }

    public static bool ValidToken(string? token) => token is { Length: 64 }
        && token.All(c => char.IsAsciiHexDigit(c) && !char.IsUpper(c));

    public Task<bool> IsExpiredAsync(string token, CancellationToken cancellationToken) =>
        sessions.IsExpiredAsync(token, cancellationToken);
}
