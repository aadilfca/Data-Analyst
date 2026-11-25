using AuditPlatform.Api.Data;

namespace AuditPlatform.Api.Tenancy;

/// <summary>
/// Coordinates tenant-scoped operations without granting the runtime role
/// UPDATE access to platform_tenants just to take a row lock.
/// </summary>
internal static class TenantWriteLock
{
    private const long ApplicationLockKey = 746102;

    public static async Task<bool> LockActiveTenantAsync(PostgresTransactionContext db, int tenantId,
        bool write, CancellationToken cancellationToken)
    {
        if (tenantId <= 0) return false;
        await AcquireAsync(db, write, cancellationToken);
        return await IsActiveAsync(db, tenantId, cancellationToken);
    }

    public static async Task AcquireAsync(PostgresTransactionContext db, bool write,
        CancellationToken cancellationToken)
    {
        var sql = write
            ? "SELECT pg_advisory_xact_lock($1)"
            : "SELECT pg_advisory_xact_lock_shared($1)";
        await using var command = db.CreateCommand(sql);
        command.Parameters.AddWithValue(ApplicationLockKey);
        await command.ExecuteNonQueryAsync(cancellationToken);
    }

    public static async Task<bool> IsActiveAsync(PostgresTransactionContext db, int tenantId,
        CancellationToken cancellationToken)
    {
        if (tenantId <= 0) return false;
        await using var command = db.CreateCommand(
            "SELECT EXISTS(SELECT 1 FROM public.platform_tenants WHERE id=$1 AND is_active IS TRUE)");
        command.Parameters.AddWithValue(tenantId);
        return (bool)(await command.ExecuteScalarAsync(cancellationToken))!;
    }
}
