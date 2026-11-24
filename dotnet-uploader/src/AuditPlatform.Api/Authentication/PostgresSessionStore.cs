using System.Diagnostics;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Authentication;

public sealed record VerifiedSession(string Token, int UserId, string Username, string? Role,
    DateTime Expiry, int? ClientId, string? ClientName, string? ClientCode,
    bool TenantContextRequired = false, int? TenantId = null, long ContextVersion = 0,
    string? TenantRole = null, bool TenantMembershipValid = false);

public sealed class PostgresSessionStore(PostgresTransactions transactions, PostgresAuditWriter audit,
    IOptions<ApplicationShellOptions>? shell = null, ILogger<PostgresSessionStore>? logger = null,
    IHostEnvironment? environment = null, PostgresSingleStatementRead? singleReads = null)
{
    public Task<bool> IsExpiredAsync(string token, CancellationToken cancellationToken) =>
        transactions.ExecuteAsync(async (db, ct) =>
        {
            await using var command = db.CreateCommand("""
                SELECT EXISTS(SELECT 1 FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                    WHERE s.token=$1 AND u.is_active IS TRUE AND s.expiry <= now())
                """);
            command.Parameters.AddWithValue(token);
            return (bool)(await command.ExecuteScalarAsync(ct))!;
        }, cancellationToken);

    public Task<VerifiedSession?> FindAsync(string token, CancellationToken cancellationToken) => singleReads is null
        ? transactions.ExecuteAsync((db, ct) => FindAsync(db.CreateCommand, token, ct), cancellationToken)
        : singleReads.ExecuteAsync("session_lookup", (db, ct) => FindAsync(db.CreateCommand, token, ct), cancellationToken);

    private Task<VerifiedSession?> FindAsync(Func<string, NpgsqlCommand> createCommand, string token, CancellationToken ct) =>
        FindCoreAsync(createCommand, token, ct);

    private async Task<VerifiedSession?> FindCoreAsync(Func<string, NpgsqlCommand> createCommand, string token, CancellationToken ct)
        {
            var scoped = shell?.Value.Enabled == true;
            await using var command = createCommand($"""
                SELECT s.user_id,u.username,u.role,s.expiry,s.selected_client_id,c.client_name,c.client_code
                    {(scoped ? ",s.selected_tenant_id,s.context_version,m.tenant_role,(m.is_active IS TRUE AND t.is_active IS TRUE)" : "")}
                FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                LEFT JOIN public.clients c ON c.id=s.selected_client_id
                {(scoped ? "LEFT JOIN public.platform_tenant_memberships m ON m.user_id=s.user_id AND m.tenant_id=s.selected_tenant_id LEFT JOIN public.platform_tenants t ON t.id=s.selected_tenant_id" : "")}
                WHERE s.token=$1 AND s.expiry > now() AND u.is_active IS TRUE
                """);
            command.Parameters.AddWithValue(token);
            var queryStarted = Stopwatch.GetTimestamp();
            try
            {
                await using var reader = await command.ExecuteReaderAsync(ct);
                if (!await reader.ReadAsync(ct)) return null;
                return new VerifiedSession(token, reader.GetInt32(0), reader.GetString(1),
                    reader.IsDBNull(2) ? null : reader.GetString(2), reader.GetDateTime(3),
                    reader.IsDBNull(5) ? null : reader.GetInt32(4),
                    reader.IsDBNull(5) ? null : reader.GetString(5),
                    reader.IsDBNull(6) ? null : reader.GetString(6), scoped,
                    scoped && !reader.IsDBNull(7) ? reader.GetInt32(7) : null, scoped ? reader.GetInt64(8) : 0,
                    scoped && !reader.IsDBNull(9) ? reader.GetString(9) : null,
                    scoped && !reader.IsDBNull(10) && reader.GetBoolean(10));
            }
            finally
            {
                if (environment?.IsDevelopment() == true)
                    logger?.LogInformation(
                        "C# PostgreSQL session lookup timing trace_id={TraceId} query_materialize_ms={QueryMs:F1}",
                        Activity.Current?.TraceId.ToString() ?? "none",
                        Stopwatch.GetElapsedTime(queryStarted).TotalMilliseconds);
            }
        }

    public Task<(string Result, string? Token, string? Username)> LoginAsync(
        string username, string password, CancellationToken cancellationToken) =>
        transactions.ExecuteAsync(async (db, ct) =>
        {
            int id; string hash; bool active;
            await using (var find = db.CreateCommand("SELECT id,password_hash,is_active FROM public.users WHERE username=$1 FOR UPDATE"))
            {
                find.Parameters.AddWithValue(username);
                await using var reader = await find.ExecuteReaderAsync(ct);
                if (!await reader.ReadAsync(ct))
                {
                    await reader.DisposeAsync();
                    await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
                        LogActor.Anonymous, null, "LoginRejected", "Authentication"), ct);
                    return ("invalid", (string?)null, (string?)null);
                }
                id = reader.GetInt32(0); hash = reader.GetString(1); active = !reader.IsDBNull(2) && reader.GetBoolean(2);
            }
            bool correct;
            var bcryptStarted = Stopwatch.GetTimestamp();
            try { correct = BCrypt.Net.BCrypt.Verify(password, hash); }
            catch (Exception error) when (error is BCrypt.Net.SaltParseException or ArgumentException) { correct = false; }
            finally
            {
                if (environment?.IsDevelopment() == true)
                    logger?.LogInformation(
                        "C# password verification timing trace_id={TraceId} verification_ms={VerificationMs:F1}",
                        Activity.Current?.TraceId.ToString() ?? "none",
                        Stopwatch.GetElapsedTime(bcryptStarted).TotalMilliseconds);
            }
            if (!correct || !active)
            {
                await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
                    LogActor.Anonymous, null, "LoginRejected", "Authentication"), ct);
                return (correct ? "inactive" : "invalid", (string?)null, (string?)null);
            }
            var token = Convert.ToHexStringLower(System.Security.Cryptography.RandomNumberGenerator.GetBytes(32));
            await using (var insert = db.CreateCommand("INSERT INTO public.sessions(token,user_id,expiry) VALUES($1,$2,now()+interval '10 hours')"))
            {
                insert.Parameters.AddWithValue(token); insert.Parameters.AddWithValue(id);
                await insert.ExecuteNonQueryAsync(ct);
            }
            await using (var update = db.CreateCommand("UPDATE public.users SET last_login=now() WHERE id=$1"))
            {
                update.Parameters.AddWithValue(id); await update.ExecuteNonQueryAsync(ct);
            }
            await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
                new LogActor("authenticated", id, username), null, "Login", "Authentication"), ct);
            return ("ok", token, username);
        }, cancellationToken);

    public Task LogoutAsync(string token, CancellationToken cancellationToken) =>
        transactions.ExecuteAsync(async (db, ct) =>
        {
            int? userId = null; string? username = null; bool verified = false;
            await using (var find = db.CreateCommand("""
                SELECT s.user_id,u.username,(u.is_active IS TRUE AND s.expiry > now())
                FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                WHERE s.token=$1 FOR UPDATE OF s
                """))
            {
                find.Parameters.AddWithValue(token);
                await using var reader = await find.ExecuteReaderAsync(ct);
                if (await reader.ReadAsync(ct))
                {
                    userId = reader.GetInt32(0); username = reader.GetString(1); verified = reader.GetBoolean(2);
                }
            }
            if (userId is not null)
            {
                await using var delete = db.CreateCommand("DELETE FROM public.sessions WHERE token=$1");
                delete.Parameters.AddWithValue(token);
                await delete.ExecuteNonQueryAsync(ct);
                await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(),
                    verified ? new LogActor("authenticated", userId, username!) : LogActor.Anonymous,
                    null, "Logout", "Authentication"), ct);
            }
            return true;
        }, cancellationToken);
}
