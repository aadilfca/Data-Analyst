using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;

namespace AuditPlatform.Api.Tenancy;

public sealed record SessionTenant(int Id, string Name, string Type, string Role);
public sealed record TenantSessionResult(bool Authenticated, IReadOnlyList<SessionTenant> Tenants,
    int? SelectedTenantId = null, string? ContextKey = null);

public sealed class TenantSessionService(PostgresTransactions transactions, PostgresAuditWriter audit)
{
    // A context assertion binds a document to its session and selection revision.
    // It is not an authentication token and is never accepted without the cookie.
    public static string ContextKey(VerifiedSession session) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(session.Token)))
        + ":" + session.ContextVersion.ToString(CultureInfo.InvariantCulture);

    public Task<TenantSessionResult> ReadAsync(VerifiedSession? session, CancellationToken ct = default) =>
        transactions.ExecuteAsync(async (db, token) =>
        {
            if (session is null || !session.TenantContextRequired) return new TenantSessionResult(false, []);
            var tenants = new List<SessionTenant>();
            // One statement rechecks the session and returns only this user's
            // current active memberships. Revoked selections have no name/role.
            await using var command = db.CreateCommand("""
                SELECT s.selected_tenant_id,s.context_version,t.id,t.name,t.tenant_type,m.tenant_role
                FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                LEFT JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.is_active IS TRUE
                LEFT JOIN public.platform_tenants t ON t.id=m.tenant_id AND t.is_active IS TRUE
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                ORDER BY t.name,t.id
                """);
            command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
            await using var reader = await command.ExecuteReaderAsync(token);
            int? selected = null; long version = 0; bool found = false;
            while (await reader.ReadAsync(token))
            {
                found = true; selected = reader.IsDBNull(0) ? null : reader.GetInt32(0); version = reader.GetInt64(1);
                if (!reader.IsDBNull(2)) tenants.Add(new(reader.GetInt32(2),reader.GetString(3),reader.GetString(4),reader.GetString(5)));
            }
            return new TenantSessionResult(found, tenants,
                tenants.Any(t=>t.Id==selected) ? selected : null, ContextKey(session with { ContextVersion = version }));
        }, ct);

    public Task<bool> SelectAsync(VerifiedSession? session, int tenantId, string expectedContext, CancellationToken ct = default) =>
        transactions.ExecuteAsync(async (db, token) =>
        {
            if (session is null || !session.TenantContextRequired || ContextKey(session) != expectedContext || tenantId <= 0) return false;
            string actorName;
            await using (var membership = db.CreateCommand("""
                SELECT u.username FROM public.sessions s JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                JOIN public.platform_tenants t ON t.id=m.tenant_id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                  AND m.tenant_id=$3 AND m.is_active IS TRUE AND t.is_active IS TRUE
                  AND s.context_version=$4 FOR UPDATE OF s FOR SHARE OF u,m,t
                """))
            {
                membership.Parameters.AddWithValue(session.Token); membership.Parameters.AddWithValue(session.UserId);
                membership.Parameters.AddWithValue(tenantId); membership.Parameters.AddWithValue(session.ContextVersion);
                if (await membership.ExecuteScalarAsync(token) is not string currentName) return false;
                actorName=currentName;
            }
            await using (var update = db.CreateCommand("""
                UPDATE public.sessions SET selected_tenant_id=$1,selected_client_id=NULL,context_version=context_version+1
                WHERE token=$2
                """))
            {
                update.Parameters.AddWithValue(tenantId); update.Parameters.AddWithValue(session.Token);
                await update.ExecuteNonQueryAsync(token);
            }
            await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(),Guid.NewGuid(),
                new LogActor("authenticated",session.UserId,actorName),null,"SelectTenant","ApplicationShell",
                JsonSerializer.SerializeToElement(new { tenant_id=tenantId })),token);
            return true;
        }, ct);
}
