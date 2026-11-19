using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Npgsql;

namespace AuditPlatform.Api.Users;

public enum TenantUserStatus { Success, Forbidden, NotFound, Conflict, Invalid, SharedIdentity, AmbiguousTenant }
public sealed record TenantUser(int Id, string Username, string Role, bool IsActive, DateTime? LastLogin);
public sealed record TenantUserResult(TenantUserStatus Status, int? UserId = null);
public sealed record TenantUserList(TenantUserStatus Status, IReadOnlyList<TenantUser> Users);
public sealed record TenantSelection(TenantUserStatus Status, int? TenantId = null);

// Tenant-scoped backend behind disabled API/shell settings.
public sealed class TenantUserService(PostgresTransactions transactions, PostgresAuditWriter audit)
{
    // P10 uses session-stored selection; without the shell, retain P08's
    // sole-membership rule. Every operation rechecks membership and context.
    public Task<TenantSelection> ResolveSoleTenantAsync(VerifiedSession? session, CancellationToken cancellationToken = default)
    {
        if (session is null || session.UserId <= 0 || !AuthenticationService.ValidToken(session.Token))
            return Task.FromResult(new TenantSelection(TenantUserStatus.Forbidden));
        return transactions.ExecuteAsync<TenantSelection>(async (db, ct) =>
        {
            var selection = session.TenantContextRequired
                ? " AND m.tenant_id=s.selected_tenant_id AND s.context_version=$3" : "";
            await using var command = db.CreateCommand($"""
                SELECT m.tenant_id,m.tenant_role FROM public.sessions s
                JOIN public.users u ON u.id=s.user_id
                JOIN public.platform_tenant_memberships m ON m.user_id=u.id
                JOIN public.platform_tenants t ON t.id=m.tenant_id
                WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
                  AND m.is_active IS TRUE AND t.is_active IS TRUE {selection} ORDER BY m.tenant_id LIMIT 2
                """);
            command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
            if (session.TenantContextRequired) command.Parameters.AddWithValue(session.ContextVersion);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return new(TenantUserStatus.Forbidden);
            var id = reader.GetInt32(0); var role = reader.GetString(1);
            if (await reader.ReadAsync(ct)) return new(TenantUserStatus.AmbiguousTenant);
            return role == "admin" ? new(TenantUserStatus.Success, id) : new(TenantUserStatus.Forbidden);
        }, cancellationToken);
    }

    public Task<TenantUserList> ListAsync(VerifiedSession? session, int tenantId, bool includeInactive,
        CancellationToken cancellationToken = default) => transactions.ExecuteAsync(async (db, ct) =>
    {
        var actor = await AuthorizeAsync(db, session, tenantId, false, ct);
        if (actor is null) return new TenantUserList(TenantUserStatus.Forbidden, []);
        await using var command = db.CreateCommand("""
            SELECT u.id,u.username,m.tenant_role,(m.is_active AND u.is_active IS TRUE),u.last_login
            FROM public.platform_tenant_memberships m JOIN public.users u ON u.id=m.user_id
            WHERE m.tenant_id=$1 AND ($2 OR (m.is_active IS TRUE AND u.is_active IS TRUE))
            ORDER BY u.username,u.id
            """);
        command.Parameters.AddWithValue(tenantId); command.Parameters.AddWithValue(includeInactive);
        await using var reader = await command.ExecuteReaderAsync(ct);
        var users = new List<TenantUser>();
        while (await reader.ReadAsync(ct))
            users.Add(new(reader.GetInt32(0), reader.GetString(1), reader.GetString(2),
                reader.GetBoolean(3), reader.IsDBNull(4) ? null : reader.GetDateTime(4)));
        return new TenantUserList(TenantUserStatus.Success, users);
    }, cancellationToken);

    public async Task<TenantUserResult> CreateAsync(VerifiedSession? session, int tenantId,
        string username, string password, string role, CancellationToken cancellationToken = default)
    {
        if (!ValidName(username) || !ValidPassword(password) || !ValidRole(role)) return new(TenantUserStatus.Invalid);
        try
        {
            return await transactions.ExecuteAsync<TenantUserResult>(async (db, ct) =>
            {
                var actor = await AuthorizeAsync(db, session, tenantId, true, ct);
                if (actor is null) return new(TenantUserStatus.Forbidden);
                var hash = BCrypt.Net.BCrypt.HashPassword(password);
                int userId;
                // Global legacy role remains NULL: a tenant admin is never a
                // Python/global administrator merely because of tenant role.
                await using (var insert = db.CreateCommand("""
                    INSERT INTO public.users(username,password_hash,is_active,role)
                    VALUES($1,$2,TRUE,NULL) RETURNING id
                    """))
                {
                    insert.Parameters.AddWithValue(username); insert.Parameters.AddWithValue(hash);
                    userId = (int)(await insert.ExecuteScalarAsync(ct))!;
                }
                await using (var membership = db.CreateCommand("""
                    INSERT INTO public.platform_tenant_memberships(tenant_id,user_id,tenant_role)
                    VALUES($1,$2,$3)
                    """))
                {
                    membership.Parameters.AddWithValue(tenantId); membership.Parameters.AddWithValue(userId);
                    membership.Parameters.AddWithValue(role); await membership.ExecuteNonQueryAsync(ct);
                }
                await AuditAsync(db, actor, tenantId, userId, "Create", ct);
                return new(TenantUserStatus.Success, userId);
            }, cancellationToken);
        }
        catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            return new(TenantUserStatus.Conflict);
        }
    }

    public async Task<TenantUserResult> UpdateAsync(VerifiedSession? session, int tenantId, int targetId,
        string username, string role, CancellationToken cancellationToken = default)
    {
        if (targetId <= 0 || !ValidName(username) || !ValidRole(role)) return new(TenantUserStatus.Invalid);
        try
        {
            return await transactions.ExecuteAsync<TenantUserResult>(async (db, ct) =>
            {
                var actor = await AuthorizeAsync(db, session, tenantId, true, ct);
                if (actor is null) return new(TenantUserStatus.Forbidden);
                var target = await TargetAsync(db, tenantId, targetId, ct);
                if (target is null) return new(TenantUserStatus.NotFound);
                if (target.GlobalRole == "admin" && (target.Username != username || target.Role != role))
                    return new(TenantUserStatus.Forbidden);
                if (target.Username != username && target.MembershipCount != 1) return new(TenantUserStatus.SharedIdentity);
                if (target.Username != username)
                {
                    await using var update = db.CreateCommand("UPDATE public.users SET username=$1 WHERE id=$2");
                    update.Parameters.AddWithValue(username); update.Parameters.AddWithValue(targetId);
                    await update.ExecuteNonQueryAsync(ct);
                }
                if (target.Role != role)
                {
                    await using var update = db.CreateCommand("UPDATE public.platform_tenant_memberships SET tenant_role=$1 WHERE tenant_id=$2 AND user_id=$3");
                    update.Parameters.AddWithValue(role); update.Parameters.AddWithValue(tenantId); update.Parameters.AddWithValue(targetId);
                    await update.ExecuteNonQueryAsync(ct);
                }
                if (target.Username != username || target.Role != role)
                    await AuditAsync(db, actor, tenantId, targetId, "Update", ct);
                return new(TenantUserStatus.Success, targetId);
            }, cancellationToken);
        }
        catch (PostgresException error) when (error.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            return new(TenantUserStatus.Conflict);
        }
    }

    public Task<TenantUserResult> SetActiveAsync(VerifiedSession? session, int tenantId, int targetId,
        bool active, CancellationToken cancellationToken = default) => transactions.ExecuteAsync<TenantUserResult>(async (db, ct) =>
    {
        var actor = await AuthorizeAsync(db, session, tenantId, true, ct);
        if (actor is null) return new(TenantUserStatus.Forbidden);
        var target = await TargetAsync(db, tenantId, targetId, ct);
        if (target is null || target.IsActive == active) return new(TenantUserStatus.NotFound);
        if (target.GlobalRole == "admin") return new(TenantUserStatus.Forbidden);
        if (active && !target.GlobalActive) return new(TenantUserStatus.Forbidden);
        await using (var update = db.CreateCommand("UPDATE public.platform_tenant_memberships SET is_active=$1 WHERE tenant_id=$2 AND user_id=$3"))
        {
            update.Parameters.AddWithValue(active); update.Parameters.AddWithValue(tenantId); update.Parameters.AddWithValue(targetId);
            await update.ExecuteNonQueryAsync(ct);
        }
        // Membership is rechecked by every tenant operation. Sessions remain
        // usable in other tenants; this never certifies legacy Python routing.
        await AuditAsync(db, actor, tenantId, targetId, active ? "Reactivate" : "Deactivate", ct);
        return new(TenantUserStatus.Success, targetId);
    }, cancellationToken);

    public Task<TenantUserResult> ResetPasswordAsync(VerifiedSession? session, int tenantId, int targetId,
        string password, CancellationToken cancellationToken = default)
    {
        if (!ValidPassword(password) || targetId <= 0) return Task.FromResult(new TenantUserResult(TenantUserStatus.Invalid));
        return transactions.ExecuteAsync<TenantUserResult>(async (db, ct) =>
        {
            var actor = await AuthorizeAsync(db, session, tenantId, true, ct);
            if (actor is null) return new(TenantUserStatus.Forbidden);
            var target = await TargetAsync(db, tenantId, targetId, ct);
            if (target is null) return new(TenantUserStatus.NotFound);
            if (target.GlobalRole == "admin") return new(TenantUserStatus.Forbidden);
            if (target.MembershipCount != 1) return new(TenantUserStatus.SharedIdentity);
            var hash = BCrypt.Net.BCrypt.HashPassword(password);
            await using (var update = db.CreateCommand("UPDATE public.users SET password_hash=$1 WHERE id=$2"))
            {
                update.Parameters.AddWithValue(hash); update.Parameters.AddWithValue(targetId);
                await update.ExecuteNonQueryAsync(ct);
            }
            await using (var invalidate = db.CreateCommand("DELETE FROM public.sessions WHERE user_id=$1"))
            {
                invalidate.Parameters.AddWithValue(targetId); await invalidate.ExecuteNonQueryAsync(ct);
            }
            await AuditAsync(db, actor, tenantId, targetId, "ResetPassword", ct);
            return new(TenantUserStatus.Success, targetId);
        }, cancellationToken);
    }

    private static async Task<LogActor?> AuthorizeAsync(PostgresTransactionContext db, VerifiedSession? session,
        int tenantId, bool lockForMutation, CancellationToken ct)
    {
        if (session is null || session.UserId <= 0 || tenantId <= 0 || !AuthenticationService.ValidToken(session.Token)) return null;
        // Serializes tenant-admin changes. The tenant id is only a selector;
        // the query proves actual membership.
        if (lockForMutation)
        {
            await using var tenantLock = db.CreateCommand("SELECT id FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE FOR UPDATE");
            tenantLock.Parameters.AddWithValue(tenantId);
            if (await tenantLock.ExecuteScalarAsync(ct) is null) return null;
        }
        var selection = session.TenantContextRequired ? " AND s.selected_tenant_id=$3 AND s.context_version=$4" : "";
        await using var command = db.CreateCommand($"""
            SELECT u.id,u.username FROM public.sessions s
            JOIN public.users u ON u.id=s.user_id
            JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            WHERE s.token=$1 AND s.user_id=$2 AND s.expiry>now() AND u.is_active IS TRUE
              AND m.tenant_id=$3 AND m.is_active IS TRUE AND m.tenant_role='admin' {selection}
              {(lockForMutation ? "FOR SHARE OF s,u,m" : "")}
            """);
        command.Parameters.AddWithValue(session.Token); command.Parameters.AddWithValue(session.UserId);
        command.Parameters.AddWithValue(tenantId);
        if (session.TenantContextRequired) command.Parameters.AddWithValue(session.ContextVersion);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? new LogActor("authenticated", reader.GetInt32(0), reader.GetString(1)) : null;
    }

    private sealed record Target(string Username, string Role, bool IsActive, bool GlobalActive, string? GlobalRole, long MembershipCount);

    private static async Task<Target?> TargetAsync(PostgresTransactionContext db, int tenantId, int targetId, CancellationToken ct)
    {
        string username; string role; bool active; bool globalActive; string? globalRole;
        await using (var command = db.CreateCommand("""
            SELECT u.username,m.tenant_role,m.is_active,(u.is_active IS TRUE),u.role
            FROM public.users u JOIN public.platform_tenant_memberships m ON m.user_id=u.id
            WHERE u.id=$1 AND m.tenant_id=$2 FOR UPDATE OF u,m
            """))
        {
            command.Parameters.AddWithValue(targetId); command.Parameters.AddWithValue(tenantId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return null;
            username = reader.GetString(0); role = reader.GetString(1); active = reader.GetBoolean(2);
            globalActive = reader.GetBoolean(3); globalRole = reader.IsDBNull(4) ? null : reader.GetString(4);
        }
        // A fresh READ COMMITTED statement after the user-row lock observes
        // membership changes that committed while we waited. The lock blocks
        // concurrent foreign-key membership inserts until this transaction ends.
        await using var count = db.CreateCommand("SELECT count(*) FROM public.platform_tenant_memberships WHERE user_id=$1");
        count.Parameters.AddWithValue(targetId);
        return new(username, role, active, globalActive, globalRole, (long)(await count.ExecuteScalarAsync(ct))!);
    }

    private Task<int> AuditAsync(PostgresTransactionContext db, LogActor actor, int tenantId, int targetId, string action, CancellationToken ct) =>
        audit.WriteAsync(db, new AuditEvent(Guid.NewGuid(), Guid.NewGuid(), actor, null, action, "TenantUsers",
            JsonSerializer.SerializeToElement(new { tenant_id = tenantId, target_user_id = targetId })), ct);

    private static bool ValidRole(string? role) => role is "admin" or "member";
    private static bool ValidName(string? value) => !string.IsNullOrWhiteSpace(value) && value.Length <= 256 && !value.Any(char.IsControl);
    private static bool ValidPassword(string? value) => !string.IsNullOrEmpty(value) && Encoding.UTF8.GetByteCount(value) <= 72;
}
