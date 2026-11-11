using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;

namespace AuditPlatform.Api.Tenancy;

// A point-in-time team eligibility result, not permission to view or modify
// client records. Callers must enforce action/object scope in their own
// transaction and recheck current access; this is not a cacheable grant.
public sealed record EngagementEligibility(int EngagementId, int FirmTenantId, int UserId, string TeamRole);

public sealed class EngagementAccessResolver(PostgresTransactions transactions)
{
    public Task<EngagementEligibility?> ResolveAsync(VerifiedSession? session, int firmTenantId,
        int engagementId, CancellationToken cancellationToken = default)
    {
        if (session is null || firmTenantId <= 0 || engagementId <= 0 || session.UserId <= 0
            || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult<EngagementEligibility?>(null);

        return transactions.ExecuteAsync(async (db, ct) =>
        {
            // Recheck everything in one query; do not accept a stale tenant
            // context, session claim or selected client as authorization.
            await using var command = db.CreateCommand("""
                SELECT e.id,e.firm_tenant_id,u.id,a.team_role
                FROM public.sessions s
                JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                JOIN public.platform_tenants t ON t.id=m.tenant_id
                JOIN public.platform_audit_engagements e ON e.firm_tenant_id=t.id
                JOIN public.platform_tenant_client_homes h
                  ON h.client_id=e.client_id AND h.tenant_id=e.firm_tenant_id
                JOIN public.clients c ON c.id=e.client_id
                JOIN public.platform_engagement_assignments a
                  ON a.engagement_id=e.id AND a.firm_tenant_id=t.id AND a.user_id=u.id
                WHERE s.token=$1 AND s.user_id=$2 AND t.id=$3 AND e.id=$4
                  AND s.expiry > now() AND u.is_active IS TRUE
                  AND m.is_active IS TRUE AND t.is_active IS TRUE
                  AND t.tenant_type='audit_firm' AND c.is_active IS TRUE
                  AND e.is_active IS TRUE AND a.is_active IS TRUE
                """);
            command.Parameters.AddWithValue(session.Token);
            command.Parameters.AddWithValue(session.UserId);
            command.Parameters.AddWithValue(firmTenantId);
            command.Parameters.AddWithValue(engagementId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return null;
            return new EngagementEligibility(reader.GetInt32(0), reader.GetInt32(1),
                reader.GetInt32(2), reader.GetString(3));
        }, cancellationToken);
    }
}
