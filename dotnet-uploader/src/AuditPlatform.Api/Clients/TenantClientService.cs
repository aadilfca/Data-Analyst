using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Clients;

public enum ClientStatus { Success, Forbidden, NotFound, Conflict, Invalid, AmbiguousTenant }
public sealed record ClientProfile(string Code, string Name, string? Industry = null,
    string? ContactPerson = null, string? Email = null, string? Phone = null, bool IsActive = true);
public sealed record TenantClient(int Id, ClientProfile Profile, DateTime CreatedAt);
public sealed record ClientResult(ClientStatus Status, IReadOnlyList<TenantClient>? Clients = null,
    long Total = 0, int? ClientId = null, string? Username = null);

// Only home-workspace records are supported. This service grants no access to
// reports, transaction data or another tenant's engagement.
public sealed class TenantClientService(PostgresTransactions transactions, PostgresAuditWriter audit)
{
    private sealed record Actor(int TenantId, int UserId, string Username, string Role);
    private const string Columns = "c.id,c.client_code,c.client_name,c.industry,c.contact_person,c.email,c.phone,c.is_active,c.created_at";
    private const string Scope = """
        h.tenant_id=$1 AND ($2='admin' OR (c.is_active IS TRUE AND EXISTS (
            SELECT 1 FROM public.platform_audit_engagements e
            JOIN public.platform_engagement_assignments a ON a.engagement_id=e.id AND a.firm_tenant_id=e.firm_tenant_id
            WHERE e.firm_tenant_id=h.tenant_id AND e.client_id=c.id AND e.is_active IS TRUE
              AND a.user_id=$3 AND a.is_active IS TRUE)))
        """;

    public Task<ClientResult> ListAsync(VerifiedSession? session, bool includeInactive, string? search,
        int? limit, int offset, CancellationToken ct = default) => transactions.ExecuteAsync(async (db, token) =>
    {
        var (status, actor) = await AuthorizeAsync(db, session, false, false, token);
        if (actor is null) return new ClientResult(status);
        if (limit is < 1 or > 200 || offset < 0) return new ClientResult(ClientStatus.Invalid);
        // One statement gives count and page the same snapshot, even when the
        // requested page is empty. No cross-tenant count is exposed.
        await using var command = db.CreateCommand($"""
            WITH matches AS MATERIALIZED (
                SELECT {Columns} FROM public.clients c
                JOIN public.platform_tenant_client_homes h ON h.client_id=c.id
                WHERE {Scope} AND ($4 OR c.is_active IS TRUE)
                AND ($5::text IS NULL OR c.client_code ILIKE $5 OR c.client_name ILIKE $5 OR c.industry ILIKE $5)
            )
            SELECT totals.total,c.* FROM (SELECT count(*) total FROM matches) totals
            LEFT JOIN LATERAL (SELECT * FROM matches ORDER BY client_name,id LIMIT $6 OFFSET $7) c ON TRUE
            ORDER BY c.client_name,c.id
            """);
        AddActor(command, actor);
        command.Parameters.AddWithValue(includeInactive);
        command.Parameters.AddWithValue(NpgsqlDbType.Text, string.IsNullOrEmpty(search) ? DBNull.Value : "%" + search + "%");
        command.Parameters.AddWithValue(NpgsqlDbType.Integer, (object?)limit ?? DBNull.Value);
        command.Parameters.AddWithValue(offset);
        await using var reader = await command.ExecuteReaderAsync(token);
        var rows = new List<TenantClient>(); long total = 0;
        while (await reader.ReadAsync(token))
        {
            total = reader.GetInt64(0);
            if (!reader.IsDBNull(1)) rows.Add(Read(reader, 1));
        }
        return new ClientResult(ClientStatus.Success, rows, total);
    }, ct);

    public async Task<ClientResult> SaveAsync(VerifiedSession? session, int? clientId, ClientProfile input,
        CancellationToken ct = default)
    {
        try
        {
            return await transactions.ExecuteAsync(async (db, token) =>
            {
                var (status, actor) = await AuthorizeAsync(db, session, true, true, token);
                if (actor is null) return new ClientResult(status);
                if (input.Code is null || input.Name is null) return new ClientResult(ClientStatus.Invalid);
                if (clientId is not null && await FindAsync(db, actor, clientId.Value, true, token) is null)
                    return new ClientResult(ClientStatus.NotFound);
                await using var command = db.CreateCommand(clientId is null ? """
                    INSERT INTO public.clients(client_code,client_name,industry,contact_person,email,phone)
                    VALUES($1,$2,$3,$4,$5,$6) RETURNING id
                    """ : """
                    UPDATE public.clients SET client_code=$1,client_name=$2,industry=$3,
                        contact_person=$4,email=$5,phone=$6,is_active=$7 WHERE id=$8 RETURNING id
                    """);
                command.Parameters.AddWithValue(input.Code); command.Parameters.AddWithValue(input.Name);
                foreach (var value in new[] { input.Industry, input.ContactPerson, input.Email, input.Phone })
                    command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)value ?? DBNull.Value);
                if (clientId is not null) { command.Parameters.AddWithValue(input.IsActive); command.Parameters.AddWithValue(clientId.Value); }
                var id = (int)(await command.ExecuteScalarAsync(token))!;
                if (clientId is null)
                {
                    await using var home = db.CreateCommand("INSERT INTO public.platform_tenant_client_homes(client_id,tenant_id) VALUES($1,$2)");
                    home.Parameters.AddWithValue(id); home.Parameters.AddWithValue(actor.TenantId);
                    await home.ExecuteNonQueryAsync(token);
                }
                if (clientId is not null && !input.IsActive) await ClearSelectionsAsync(db, id, token);
                await AuditAsync(db, actor, id, clientId is null ? "Create" : "Update", token);
                return new ClientResult(ClientStatus.Success, ClientId: id);
            }, ct);
        }
        catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            // Global legacy uniqueness remains. Never reveal the other tenant.
            return new(ClientStatus.Conflict);
        }
    }

    public Task<ClientResult> SetActiveAsync(VerifiedSession? session, int id, bool active, CancellationToken ct = default) =>
        transactions.ExecuteAsync(async (db, token) =>
        {
            var (status, actor) = await AuthorizeAsync(db, session, true, true, token);
            if (actor is null) return new ClientResult(status);
            var client = await FindAsync(db, actor, id, true, token);
            if (client is null || client.Profile.IsActive == active) return new ClientResult(ClientStatus.NotFound);
            await using var command = db.CreateCommand("UPDATE public.clients SET is_active=$1 WHERE id=$2");
            command.Parameters.AddWithValue(active); command.Parameters.AddWithValue(id);
            await command.ExecuteNonQueryAsync(token);
            if (!active) await ClearSelectionsAsync(db, id, token);
            await AuditAsync(db, actor, id, active ? "Restore" : "Delete", token);
            return new ClientResult(ClientStatus.Success, ClientId: id);
        }, ct);

    public Task<ClientResult> GetAsync(VerifiedSession? session, int id, CancellationToken ct = default) =>
        transactions.ExecuteAsync(async (db, token) =>
        {
            var (status, actor) = await AuthorizeAsync(db, session, true, false, token);
            if (actor is null) return new ClientResult(status);
            var client = await FindAsync(db, actor, id, false, token);
            return client is null ? new(ClientStatus.NotFound) : new ClientResult(ClientStatus.Success, [client]);
        }, ct);

    public Task<ClientResult> SelectionAsync(VerifiedSession? session, int? requestedId, CancellationToken ct = default) =>
        transactions.ExecuteAsync(async (db, token) =>
        {
            var (status, actor) = await AuthorizeAsync(db, session, false, false, token);
            if (actor is null) return new ClientResult(status);
            // Lock the real session; never accept a cached selected-client claim.
            int? selected;
            await using (var command = db.CreateCommand("SELECT selected_client_id FROM public.sessions WHERE token=$1 FOR UPDATE"))
            {
                command.Parameters.AddWithValue(session!.Token);
                var value = await command.ExecuteScalarAsync(token);
                selected = value is int id ? id : null;
            }
            var targetId = requestedId ?? selected;
            var client = targetId is null ? null : await FindAsync(db, actor, targetId.Value, false, token);
            if (client is not null && !client.Profile.IsActive) client = null;
            // Verify the current assignment before saving selection. Every later
            // request rechecks membership and assignment against current data.
            if (client is not null && actor.Role != "admin")
            {
                await using var assignment = db.CreateCommand("""
                    SELECT a.user_id FROM public.platform_engagement_assignments a
                    JOIN public.platform_audit_engagements e ON e.id=a.engagement_id AND e.firm_tenant_id=a.firm_tenant_id
                    WHERE a.user_id=$1 AND a.firm_tenant_id=$2 AND e.client_id=$3
                      AND a.is_active IS TRUE AND e.is_active IS TRUE
                    """);
                assignment.Parameters.AddWithValue(actor.UserId); assignment.Parameters.AddWithValue(actor.TenantId);
                assignment.Parameters.AddWithValue(client.Id);
                if (await assignment.ExecuteScalarAsync(token) is null) client = null;
            }
            if (requestedId is not null && client is null) return new ClientResult(ClientStatus.NotFound);
            if (requestedId is not null || (selected is not null && client is null))
            {
                await using var update = db.CreateCommand("UPDATE public.sessions SET selected_client_id=$1 WHERE token=$2");
                update.Parameters.AddWithValue(NpgsqlDbType.Integer, client is null ? DBNull.Value : client.Id);
                update.Parameters.AddWithValue(session!.Token); await update.ExecuteNonQueryAsync(token);
                await AuditAsync(db, actor, client?.Id ?? selected!.Value, requestedId is null ? "ClearSelection" : "Select", token);
            }
            return new ClientResult(ClientStatus.Success, client is null ? [] : [client], Username: actor.Username);
        }, ct);

    private static async Task<(ClientStatus, Actor?)> AuthorizeAsync(PostgresTransactionContext db,
        VerifiedSession? session, bool admin, bool lockForMutation, CancellationToken ct)
    {
        if (session is null || session.UserId <= 0 || !AuthenticationService.ValidToken(session.Token)) return (ClientStatus.Forbidden, null);
        int tenant;
        if (session.TenantContextRequired)
        {
            if (session.TenantId is null) return (ClientStatus.AmbiguousTenant, null);
            tenant = session.TenantId.Value;
        }
        else
        await using (var selection = db.CreateCommand("""
            SELECT m.tenant_id FROM public.platform_tenant_memberships m JOIN public.platform_tenants t ON t.id=m.tenant_id
            WHERE m.user_id=$1 AND m.is_active IS TRUE AND t.is_active IS TRUE ORDER BY m.tenant_id LIMIT 2
            """))
        {
            selection.Parameters.AddWithValue(session.UserId);
            await using var reader = await selection.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return (ClientStatus.Forbidden, null);
            tenant = reader.GetInt32(0);
            if (await reader.ReadAsync(ct)) return (ClientStatus.AmbiguousTenant, null);
        }
        // Same lock order as tenant user administration; it serializes tenant
        // mutations and prevents role/membership revocation during this action.
        if (lockForMutation)
        {
            await using var tenantLock = db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE");
            tenantLock.Parameters.AddWithValue(tenant);
            if (await tenantLock.ExecuteScalarAsync(ct) is null) return (ClientStatus.Forbidden, null);
        }
        var contextCheck = session.TenantContextRequired ? " AND s.selected_tenant_id=$3 AND s.context_version=$4" : "";
        await using var command = db.CreateCommand($"""
            SELECT u.id,u.username,m.tenant_role FROM public.sessions s JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                AND m.tenant_id=$3 AND m.is_active IS TRUE {contextCheck}{(lockForMutation ? " FOR SHARE OF s,u,m" : " FOR SHARE OF s,u")}
            """);
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId); command.Parameters.AddWithValue(tenant);
        if (session.TenantContextRequired) command.Parameters.AddWithValue(session.ContextVersion);
        await using var actor = await command.ExecuteReaderAsync(ct);
        if (!await actor.ReadAsync(ct) || (admin && actor.GetString(2) != "admin")) return (ClientStatus.Forbidden, null);
        return (ClientStatus.Success, new Actor(tenant, actor.GetInt32(0), actor.GetString(1), actor.GetString(2)));
    }

    private static async Task<TenantClient?> FindAsync(PostgresTransactionContext db, Actor actor, int id,
        bool lockForMutation, CancellationToken ct)
    {
        await using var command = db.CreateCommand($"SELECT {Columns} FROM public.clients c JOIN public.platform_tenant_client_homes h ON h.client_id=c.id WHERE {Scope} AND c.id=$4{(lockForMutation ? " FOR UPDATE OF c,h" : "")}");
        AddActor(command, actor); command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? Read(reader, 0) : null;
    }

    private static void AddActor(NpgsqlCommand command, Actor actor)
    {
        command.Parameters.AddWithValue(actor.TenantId); command.Parameters.AddWithValue(actor.Role); command.Parameters.AddWithValue(actor.UserId);
    }
    private static TenantClient Read(NpgsqlDataReader reader, int start) => new(reader.GetInt32(start),
        new(reader.GetString(start+1), reader.GetString(start+2), Text(reader,start+3), Text(reader,start+4),
            Text(reader,start+5), Text(reader,start+6), !reader.IsDBNull(start+7) && reader.GetBoolean(start+7)), reader.GetDateTime(start+8));
    private static string? Text(NpgsqlDataReader reader, int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
    private static async Task ClearSelectionsAsync(PostgresTransactionContext db, int id, CancellationToken ct)
    {
        await using var command = db.CreateCommand("UPDATE public.sessions SET selected_client_id=NULL WHERE selected_client_id=$1");
        command.Parameters.AddWithValue(id); await command.ExecuteNonQueryAsync(ct);
    }
    private Task<int> AuditAsync(PostgresTransactionContext db, Actor actor, int id, string action, CancellationToken ct) =>
        audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), new LogActor("authenticated", actor.UserId, actor.Username),
            id, action, "TenantClients", JsonSerializer.SerializeToElement(new { tenant_id = actor.TenantId, record_id = id })), ct);
}
