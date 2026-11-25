using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Tenancy;

namespace AuditPlatform.Api.Dashboard;

internal sealed class WorkspaceRejected(string code, int status = 403) : Exception(code)
{
    public int Status { get; } = status;
}

// Firm-level reads need a validated workspace, without changing the selected client.
internal static class WorkspaceReadScope
{
    internal sealed record Actor(int TenantId, int UserId, string Name, bool Admin);
    internal static async Task<Actor> Authorize(PostgresTransactionContext db, VerifiedSession? session,
        bool admin, CancellationToken ct, bool writing = false)
    {
        if (session is null) throw new WorkspaceRejected("not_authenticated",401);
        if (!session.TenantContextRequired || session.TenantId is not > 0 || !AuthenticationService.ValidToken(session.Token))
            throw new WorkspaceRejected("workspace_access_denied");
        // Match the established tenant-first write lock order. Reads stay SELECT-only.
        if (writing)
        {
            if (!await TenantWriteLock.LockActiveTenantAsync(db, session.TenantId.Value, true, ct))
                throw new WorkspaceRejected("workspace_access_denied");
        }
        await using var command = db.CreateCommand("""
            SELECT u.username,m.tenant_role FROM public.sessions s
            JOIN public.users u ON u.id=s.user_id AND u.is_active IS TRUE
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id AND m.tenant_id=$3 AND m.is_active IS TRUE
            JOIN public.platform_tenants t ON t.id=m.tenant_id AND t.is_active IS TRUE
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now()
              AND s.selected_tenant_id=$3 AND s.context_version=$4
            """ + (writing ? " FOR SHARE OF s,u,m" : ""));
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(session.TenantId.Value); command.Parameters.AddWithValue(session.ContextVersion);
        await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct) || admin && reader.GetString(1)!="admin") throw new WorkspaceRejected("workspace_access_denied");
        return new(session.TenantId.Value,session.UserId,reader.GetString(0),reader.GetString(1)=="admin");
    }

    // Do not infer sharing or grant business members access through a Dashboard total.
    internal const string Clients = """
        WITH allowed_clients AS (
            SELECT c.id,c.client_name FROM public.clients c
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$1
            JOIN public.platform_tenants t ON t.id=h.tenant_id
            WHERE c.is_active IS TRUE AND ($3 OR (t.tenant_type='audit_firm' AND EXISTS (
                SELECT 1 FROM public.platform_audit_engagements e JOIN public.platform_engagement_assignments a
                ON a.engagement_id=e.id AND a.firm_tenant_id=e.firm_tenant_id
                WHERE e.client_id=c.id AND e.firm_tenant_id=$1 AND e.is_active IS TRUE
                  AND a.user_id=$2 AND a.is_active IS TRUE AND a.team_role IN ('manager','internee')))))
        """;
    internal static void Parameters(Npgsql.NpgsqlCommand command, Actor actor)
    { command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(actor.UserId); command.Parameters.AddWithValue(actor.Admin); }
}
