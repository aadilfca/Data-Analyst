using System.Text.Json;
using System.Text.Json.Serialization;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Tenancy;
using Npgsql;

namespace AuditPlatform.Api.Users;

public enum ClientAssignmentStatus { Success, Forbidden, NotFound, Invalid }
public sealed record TenantClientAssignment(int ClientId, string ClientCode, string ClientName,
    bool IsAssigned, string? TeamRole);
public sealed record TenantClientAssignmentList(ClientAssignmentStatus Status,
    IReadOnlyList<TenantClientAssignment> Assignments);
public sealed record ClientAssignmentInput(
    [property: JsonPropertyName("client_id")] int ClientId,
    [property: JsonPropertyName("team_role")] string? TeamRole);

// The Admin UI manages tenant-scoped client assignments, not global user roles.
public sealed class TenantClientAssignmentService(PostgresTransactions transactions, PostgresAuditWriter audit)
{
    public Task<TenantClientAssignmentList> ListAsync(VerifiedSession? session, int tenantId, int userId,
        CancellationToken cancellationToken = default) => transactions.ExecuteAsync<TenantClientAssignmentList>(async (db, ct) =>
    {
        var actor = await AuthorizeAsync(db, session, tenantId, false, ct);
        if (actor is null) return new(ClientAssignmentStatus.Forbidden, []);
        var targetRole = await TargetRoleAsync(db, tenantId, userId, ct);
        if (targetRole is null) return new(ClientAssignmentStatus.NotFound, []);
        if (targetRole == "admin") return new(ClientAssignmentStatus.Invalid, []);

        await using var command = db.CreateCommand("""
            SELECT c.id,c.client_code,c.client_name,
                   COALESCE(a.is_active,FALSE),a.team_role
            FROM public.clients c
            JOIN public.platform_tenant_client_homes h ON h.client_id=c.id AND h.tenant_id=$1
            LEFT JOIN public.platform_client_user_assignments a
              ON a.tenant_id=h.tenant_id AND a.client_id=c.id AND a.user_id=$2
            WHERE c.is_active IS TRUE
            ORDER BY lower(c.client_name),c.id
            """);
        command.Parameters.AddWithValue(tenantId);
        command.Parameters.AddWithValue(userId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        var result = new List<TenantClientAssignment>();
        while (await reader.ReadAsync(ct))
            result.Add(new(reader.GetInt32(0), reader.GetString(1), reader.GetString(2),
                reader.GetBoolean(3), reader.IsDBNull(4) ? null : reader.GetString(4)));
        return new(ClientAssignmentStatus.Success, result);
    }, cancellationToken);

    public Task<ClientAssignmentStatus> SaveAsync(VerifiedSession? session, int tenantId, int userId,
        IReadOnlyList<ClientAssignmentInput>? assignments, CancellationToken cancellationToken = default)
    {
        if (assignments is null || assignments.Count > 2000
            || assignments.Any(a => a.ClientId <= 0 || a.TeamRole is not ("manager" or "internee"))
            || assignments.Select(a => a.ClientId).Distinct().Count() != assignments.Count)
            return Task.FromResult(ClientAssignmentStatus.Invalid);

        return transactions.ExecuteAsync(async (db, ct) =>
        {
            var actor = await AuthorizeAsync(db, session, tenantId, true, ct);
            if (actor is null) return ClientAssignmentStatus.Forbidden;
            var targetRole = await TargetRoleAsync(db, tenantId, userId, ct);
            if (targetRole is null) return ClientAssignmentStatus.NotFound;
            if (targetRole == "admin") return ClientAssignmentStatus.Invalid;

            var clientIds = assignments.Select(a => a.ClientId).ToArray();
            if (clientIds.Length > 0)
            {
                await using var validClients = db.CreateCommand("""
                    SELECT count(*) FROM public.clients c
                    JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
                    WHERE h.tenant_id=$1 AND c.id=ANY($2::integer[]) AND c.is_active IS TRUE
                    """);
                validClients.Parameters.AddWithValue(tenantId);
                validClients.Parameters.AddWithValue(clientIds);
                if ((long)(await validClients.ExecuteScalarAsync(ct))! != clientIds.Length)
                    return ClientAssignmentStatus.Invalid;

                var roles = assignments.Select(a => a.TeamRole!).ToArray();
                await using var upsert = db.CreateCommand("""
                    INSERT INTO public.platform_client_user_assignments
                        (tenant_id,client_id,user_id,team_role)
                    SELECT $1,x.client_id,$2,x.team_role
                    FROM unnest($3::integer[],$4::text[]) AS x(client_id,team_role)
                    ON CONFLICT (tenant_id,client_id,user_id)
                    DO UPDATE SET team_role=EXCLUDED.team_role,is_active=TRUE
                    """);
                upsert.Parameters.AddWithValue(tenantId);
                upsert.Parameters.AddWithValue(userId);
                upsert.Parameters.AddWithValue(clientIds);
                upsert.Parameters.AddWithValue(roles);
                await upsert.ExecuteNonQueryAsync(ct);
            }

            await using (var deactivate = db.CreateCommand("""
                UPDATE public.platform_client_user_assignments SET is_active=FALSE
                WHERE tenant_id=$1 AND user_id=$2 AND is_active IS TRUE
                  AND NOT (client_id=ANY($3::integer[]))
                """))
            {
                deactivate.Parameters.AddWithValue(tenantId);
                deactivate.Parameters.AddWithValue(userId);
                deactivate.Parameters.AddWithValue(clientIds);
                await deactivate.ExecuteNonQueryAsync(ct);
            }

            await audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), actor, null,
                "AssignClients", "TenantUsers", JsonSerializer.SerializeToElement(new
                {
                    tenant_id = tenantId,
                    target_user_id = userId,
                    assignments = assignments.Select(a => new
                    {
                        client_id = a.ClientId,
                        team_role = a.TeamRole
                    }).ToArray()
                })), ct);
            return ClientAssignmentStatus.Success;
        }, cancellationToken);
    }

    private static async Task<LogActor?> AuthorizeAsync(PostgresTransactionContext db, VerifiedSession? session,
        int tenantId, bool writing, CancellationToken ct)
    {
        if (session is null || session.UserId <= 0 || tenantId <= 0
            || !AuthenticationService.ValidToken(session.Token)) return null;
        if (writing && !await TenantWriteLock.LockActiveTenantAsync(db, tenantId, true, ct)) return null;

        var selection = session.TenantContextRequired ? " AND s.selected_tenant_id=$3 AND s.context_version=$4" : "";
        await using var command = db.CreateCommand($"""
            SELECT u.id,u.username FROM public.sessions s
            JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
              AND m.tenant_id=$3 AND m.is_active IS TRUE AND m.tenant_role='admin' {selection}
            """);
        command.Parameters.AddWithValue(session.Token);
        command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(tenantId);
        if (session.TenantContextRequired) command.Parameters.AddWithValue(session.ContextVersion);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct)
            ? new LogActor("authenticated", reader.GetInt32(0), reader.GetString(1)) : null;
    }

    private static async Task<string?> TargetRoleAsync(PostgresTransactionContext db, int tenantId, int userId,
        CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT m.tenant_role FROM public.platform_tenant_memberships m
            JOIN public.users u ON u.id=m.user_id
            WHERE m.tenant_id=$1 AND m.user_id=$2 AND m.is_active IS TRUE AND u.is_active IS TRUE
            """);
        command.Parameters.AddWithValue(tenantId);
        command.Parameters.AddWithValue(userId);
        return await command.ExecuteScalarAsync(ct) as string;
    }
}
