using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;

namespace AuditPlatform.Api.Tenancy;

// This proves tenant membership at lookup time only. It grants no client,
// engagement, report or sharing permission and has no public endpoint.
// Future operations must recheck current authorization within their own scope;
// a cached context cannot outlive a membership or sharing change.
public sealed record TenantContext(int TenantId, string TenantType, int UserId);

public sealed class TenantContextResolver(PostgresTransactions transactions)
{
    public Task<TenantContext?> ResolveAsync(VerifiedSession? session, int tenantId,
        CancellationToken cancellationToken = default)
    {
        if (session is null || tenantId <= 0 || session.UserId <= 0
            || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult<TenantContext?>(null);

        return transactions.ExecuteAsync(async (db, ct) =>
        {
            // Recheck session and membership together; callers cannot create
            // a tenant context by supplying a tenant ID or stale session.
            await using var command = db.CreateCommand("""
                SELECT t.id,t.tenant_type,u.id
                FROM public.sessions s
                JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                JOIN public.platform_tenants t ON t.id=m.tenant_id
                WHERE s.token=$1 AND s.user_id=$2 AND t.id=$3
                  AND s.expiry > now() AND u.is_active IS TRUE
                  AND m.is_active IS TRUE AND t.is_active IS TRUE
                """);
            command.Parameters.AddWithValue(session.Token);
            command.Parameters.AddWithValue(session.UserId);
            command.Parameters.AddWithValue(tenantId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return null;
            return new TenantContext(reader.GetInt32(0), reader.GetString(1), reader.GetInt32(2));
        }, cancellationToken);
    }
}
