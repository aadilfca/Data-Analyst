using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.Api.Logging;

public sealed class PostgresAuditWriter(LogSanitizer sanitizer, IOptions<LogPersistenceOptions> options)
{
    public async Task<int> WriteAsync(PostgresTransactionContext context, AuditEvent record,
        CancellationToken cancellationToken = default)
    {
        Validate(record);
        var details = sanitizer.Metadata(record.Metadata, options.Value.AuditDetailsMaxCharacters);
        await using var command = context.CreateCommand("""
            INSERT INTO public.audit_log("user",action,module,details,event_id,operation_id,
                actor_user_id,actor_kind,client_id,correlation_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id
            """);
        Add(command, NpgsqlDbType.Text, sanitizer.Text(record.Actor.Name, 256));
        Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Action));
        Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Module));
        Add(command, NpgsqlDbType.Text, details);
        AddIdentity(command, record);
        return (int)(await command.ExecuteScalarAsync(cancellationToken))!;
    }

    public async Task<int> WriteDataChangeAsync(PostgresTransactionContext context, DataChangeAudit record,
        CancellationToken cancellationToken = default)
    {
        Validate(record.Event);
        if (record.ClientDataId <= 0 || record.Event.ClientId is not > 0)
            throw new ArgumentException("Data audit requires a validated client and source row.");
        // Validate row ownership using the same transaction, not another lease.
        await using (var scope = context.CreateCommand("SELECT client_id FROM public.client_data WHERE id=$1 FOR KEY SHARE"))
        {
            scope.Parameters.AddWithValue(record.ClientDataId);
            if (await scope.ExecuteScalarAsync(cancellationToken) is not int client || client != record.Event.ClientId)
                throw new InvalidOperationException("Data audit source does not match its authorized client.");
        }
        var compact = sanitizer.Compact(record);
        await using var command = context.CreateCommand("""
            INSERT INTO public.data_audit_log(client_data_id,"user",action,old_row_data,new_row_data,
                event_id,operation_id,actor_user_id,actor_kind,client_id,correlation_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id
            """);
        Add(command, NpgsqlDbType.Integer, record.ClientDataId);
        Add(command, NpgsqlDbType.Text, sanitizer.Text(record.Event.Actor.Name, 256));
        Add(command, NpgsqlDbType.Text, sanitizer.Label(record.Event.Action));
        Add(command, NpgsqlDbType.Jsonb, compact.Old);
        Add(command, NpgsqlDbType.Jsonb, compact.New);
        AddIdentity(command, record.Event);
        return (int)(await command.ExecuteScalarAsync(cancellationToken))!;
    }

    private void Validate(AuditEvent record)
    {
        if (record.EventId == Guid.Empty || record.OperationId == Guid.Empty || record.ClientId is <= 0)
            throw new ArgumentException("Audit identity or scope is invalid.");
        sanitizer.ValidateActor(record.Actor);
        _ = sanitizer.Label(record.Action);
        _ = sanitizer.Label(record.Module);
        if (record.CorrelationId is not null) _ = sanitizer.Label(record.CorrelationId, 64);
    }

    private static void AddIdentity(NpgsqlCommand command, AuditEvent record)
    {
        Add(command, NpgsqlDbType.Uuid, record.EventId);
        Add(command, NpgsqlDbType.Uuid, record.OperationId);
        Add(command, NpgsqlDbType.Integer, record.Actor.UserId);
        Add(command, NpgsqlDbType.Text, record.Actor.Kind);
        Add(command, NpgsqlDbType.Integer, record.ClientId);
        Add(command, NpgsqlDbType.Text, record.CorrelationId);
    }

    internal static void Add(NpgsqlCommand command, NpgsqlDbType type, object? value) =>
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = type, Value = value ?? DBNull.Value });
}
