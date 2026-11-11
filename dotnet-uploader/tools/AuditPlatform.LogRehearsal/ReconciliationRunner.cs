using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using NpgsqlTypes;

namespace AuditPlatform.LogRehearsal;

public sealed class ReconciliationRunner(RehearsalTarget target)
{
    private static readonly string[] Kinds = ["error_codes", "audit_log", "data_audit_log", "error_log"];
    private static readonly HashSet<string> PublicFields = new(StringComparer.Ordinal)
        { "error_codes.user_message", "error_codes.suggested_action", "error_log.resolution_notes" };
    private readonly LogSanitizer sanitizer = new(Options.Create(new LogPersistenceOptions()));
    private sealed record Field(NpgsqlDbType Type, object? Value);
    private sealed class Projection
    {
        public Dictionary<string, Field> Fields { get; } = new(StringComparer.Ordinal);
        public Dictionary<string, string> Times { get; } = new(StringComparer.Ordinal);
        public int Redactions { get; set; }
    }
    private sealed class RecordIssue(string code) : Exception
    {
        public string Code { get; } = code;
    }

    public async Task<ReconciliationReport> RunAsync(ReconciliationManifest manifest,
        IProgress<ReconciliationProgress>? progress = null, CancellationToken cancellationToken = default)
    {
        Validate(manifest);
        // Freeze mutable caller collections and reject duplicate JSON keys before
        // any record writes; observers cannot change policy partway through a run.
        manifest = ReconciliationJson.Parse(JsonSerializer.SerializeToUtf8Bytes(manifest, ReconciliationJson.Options));
        var items = new List<ReconciliationItem>();
        var status = "completed";
        foreach (var record in manifest.Records.OrderBy(r => Array.IndexOf(Kinds, r.Kind)).ThenBy(r => r.SourceId))
        {
            if (cancellationToken.IsCancellationRequested) { status = "cancelled"; break; }
            var item = await ProcessAsync(manifest, record, cancellationToken);
            items.Add(item);
            // A progress observer is not allowed to change committed truth.
            try { progress?.Report(new(items.Count, manifest.Records.Count)); }
            catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException) { }
            if (item.Status is "unknown" or "failed" or "cancelled") { status = item.Status; break; }
        }
        if (status == "completed" && items.Any(item => item.Status is "conflict" or "quarantined")) status = "conflicts";
        return new(manifest.OriginId, manifest.SnapshotId, status, items, manifest.Records.Count);
    }

    private static void Validate(ReconciliationManifest manifest)
    {
        if (manifest.Version != 1 || manifest.SourceKind != "synthetic"
            || manifest.OriginId == Guid.Empty || manifest.SnapshotId == Guid.Empty
            || manifest.Records is null || manifest.Records.Count > 1000
            || manifest.NaiveTimeZones is null || manifest.ClientIds is null || manifest.CanonicalRows is null
            || manifest.ValueFields is null || manifest.PublicTextFields is null
            || manifest.PublicTextFields.Any(field => !PublicFields.Contains(field))
            || manifest.ValueFields.Count > 1000 || manifest.NaiveTimeZones.Count > 20
            || manifest.ClientIds.Count > 1000 || manifest.CanonicalRows.Count > 1000
            || manifest.Records.Any(r => r is null || !Kinds.Contains(r.Kind) || r.SourceId <= 0
                || r.SourceId == int.MaxValue || r.Values.ValueKind != JsonValueKind.Object
                || r.Values.GetRawText().Length > 100000 || r.PayloadMode is not ("compact" or "full_rows"))
            || manifest.Records.Select(r => (r.Kind, r.SourceId)).Distinct().Count() != manifest.Records.Count
            || manifest.ClientIds.Any(p => p.Key <= 0 || p.Value <= 0)
            || manifest.CanonicalRows.Any(p => p.Key <= 0 || p.Value is null || p.Value.TargetId <= 0 || p.Value.SourceClientId <= 0))
            throw new ArgumentException("Invalid synthetic reconciliation manifest.");
    }

    private async Task<ReconciliationItem> ProcessAsync(ReconciliationManifest manifest, LegacyLogRecord record, CancellationToken token)
    {
        NpgsqlConnection? connection = null;
        NpgsqlTransaction? transaction = null;
        var committing = false;
        try
        {
            connection = await target.Source.OpenConnectionAsync(token);
            transaction = await connection.BeginTransactionAsync(token);
            await target.VerifyAsync(connection, transaction, token);
            await ExecuteAsync(connection, transaction,
                "SELECT pg_advisory_xact_lock(589530501); SET LOCAL lock_timeout='3s'; SET LOCAL statement_timeout='5s'", token);
            var projection = await PrepareAsync(manifest, record, connection, transaction, token);
            var hash = SourceHash(manifest, record);
            await using (var checkpoint = Command(connection, transaction, """
                SELECT target_id,source_hash,target_hash,redactions FROM public.log_reconciliation_map
                WHERE source_origin=$1 AND source_kind=$2 AND source_record_id=$3
                """, (NpgsqlDbType.Uuid, manifest.OriginId), (NpgsqlDbType.Text, record.Kind), (NpgsqlDbType.Integer, record.SourceId)))
            {
                int? existing = null;
                string? sourceHash = null, expected = null;
                var redactions = 0;
                await using (var reader = await checkpoint.ExecuteReaderAsync(token))
                    if (await reader.ReadAsync(token))
                    {
                        existing = reader.GetInt32(0); sourceHash = reader.GetString(1);
                        expected = reader.GetString(2); redactions = reader.GetInt32(3);
                    }
                if (existing is not null)
                {
                    var current = await TargetHashAsync(record.Kind, projection.Fields.Keys, existing.Value, connection, transaction, token);
                    await transaction.RollbackAsync(token);
                    return sourceHash != hash ? new(record.Kind, record.SourceId, existing, "conflict", "source_changed")
                        : current != expected ? new(record.Kind, record.SourceId, existing, "conflict", "target_changed")
                        : new(record.Kind, record.SourceId, existing, "already_applied", "verified_checkpoint", redactions);
                }
            }

            await ExecuteAsync(connection, transaction, $"LOCK TABLE public.{record.Kind} IN SHARE ROW EXCLUSIVE MODE", token);
            int? id = null;
            var result = "imported";
            if (record.Kind == "error_codes")
            {
                await using var find = Command(connection, transaction, "SELECT id FROM public.error_codes WHERE code=$1",
                    (NpgsqlDbType.Text, projection.Fields["code"].Value));
                var value = await find.ExecuteScalarAsync(token);
                if (value is int candidate)
                {
                    if (!await RegistryMatchesAsync(projection, candidate, connection, transaction, token))
                        throw new RecordIssue("registry_conflict");
                    id = candidate; result = "matched";
                }
            }
            if (id is null)
            {
                await using var occupied = Command(connection, transaction, $"SELECT EXISTS(SELECT 1 FROM public.{record.Kind} WHERE id=$1)",
                    (NpgsqlDbType.Integer, record.SourceId));
                var collision = (bool)(await occupied.ExecuteScalarAsync(token))!;
                await using var sequence = Command(connection, transaction,
                    "SELECT nextval(pg_get_serial_sequence($1,'id'))", (NpgsqlDbType.Text, "public." + record.Kind));
                var next = (long)(await sequence.ExecuteScalarAsync(token))!;
                await using var maximum = Command(connection, transaction, $"SELECT COALESCE(max(id),0) FROM public.{record.Kind}");
                var max = Convert.ToInt64(await maximum.ExecuteScalarAsync(token), CultureInfo.InvariantCulture);
                var chosen = collision ? Math.Max(next, max + 1) : record.SourceId;
                if (chosen >= int.MaxValue) throw new RecordIssue("identity_capacity");
                id = (int)chosen;
                var fields = new Dictionary<string, Field>(projection.Fields) { ["id"] = new(NpgsqlDbType.Integer, id) };
                var columns = string.Join(',', fields.Keys.Select(k => '"' + k + '"'));
                var parameters = string.Join(',', Enumerable.Range(1, fields.Count).Select(i => "$" + i));
                await using var insert = Command(connection, transaction,
                    $"INSERT INTO public.{record.Kind}({columns}) OVERRIDING SYSTEM VALUE VALUES ({parameters})",
                    fields.Values.Select(f => (f.Type, f.Value)).ToArray());
                await insert.ExecuteNonQueryAsync(token);
                await using var advance = Command(connection, transaction,
                    "SELECT setval(pg_get_serial_sequence($1,'id'),$2,true)",
                    (NpgsqlDbType.Text, "public." + record.Kind), (NpgsqlDbType.Bigint, Math.Max(next, Math.Max(chosen, max))));
                await advance.ExecuteScalarAsync(token);
            }
            var targetHash = await TargetHashAsync(record.Kind, projection.Fields.Keys, id.Value, connection, transaction, token)
                ?? throw new InvalidOperationException("Inserted target record is unavailable.");
            await using var receipt = Command(connection, transaction, """
                INSERT INTO public.log_reconciliation_map(source_origin,source_kind,source_record_id,target_id,
                    source_hash,target_hash,result,redactions,source_times) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """, (NpgsqlDbType.Uuid, manifest.OriginId), (NpgsqlDbType.Text, record.Kind),
                (NpgsqlDbType.Integer, record.SourceId), (NpgsqlDbType.Integer, id.Value),
                (NpgsqlDbType.Text, hash), (NpgsqlDbType.Text, targetHash), (NpgsqlDbType.Text, result),
                (NpgsqlDbType.Integer, projection.Redactions), (NpgsqlDbType.Jsonb, JsonSerializer.Serialize(projection.Times)));
            await receipt.ExecuteNonQueryAsync(token);
            token.ThrowIfCancellationRequested();
            committing = true;
            await transaction.CommitAsync(token);
            return new(record.Kind, record.SourceId, id, result, "verified", projection.Redactions);
        }
        catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException)
        {
            if (transaction is not null)
            {
                using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                try { await transaction.RollbackAsync(cleanup.Token); }
                catch (Exception rollback) when (rollback is NpgsqlException or OperationCanceledException or InvalidOperationException or TimeoutException) { }
            }
            var uncertain = committing && (error is not PostgresException server
                || server.SqlState.StartsWith("08", StringComparison.Ordinal) || server.SqlState is "57P01" or "57P02");
            return uncertain ? new(record.Kind, record.SourceId, null, "unknown", "reconcile_before_retry")
                : error is RecordIssue issue ? new(record.Kind, record.SourceId, null, "conflict", issue.Code)
                : error is OperationCanceledException ? new(record.Kind, record.SourceId, null, "cancelled", "cancelled")
                : error is ArgumentException or JsonException or InvalidOperationException or FormatException or OverflowException
                    ? new(record.Kind, record.SourceId, null, "quarantined", "invalid_record")
                    : new(record.Kind, record.SourceId, null, "failed", "database_failure");
        }
        finally
        {
            if (transaction is not null) await transaction.DisposeAsync();
            if (connection is not null) await connection.DisposeAsync();
        }
    }

    private async Task<Projection> PrepareAsync(ReconciliationManifest manifest, LegacyLogRecord record,
        NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken token)
    {
        var p = new Projection();
        var values = record.Values;
        if (HasValue(values, "id") && ReadInt(values, "id") != record.SourceId) throw new RecordIssue("source_identity_conflict");
        var known = record.Kind switch
        {
            "error_codes" => new[] { "id", "code", "severity", "category", "user_message", "tech_description", "suggested_action", "is_active", "created_at" },
            "audit_log" => ["id", "user", "action", "module", "details", "timestamp", "client_id"],
            "data_audit_log" => ["id", "client_data_id", "user", "action", "old_row_data", "new_row_data", "changed_at"],
            _ => ["id", "error_code_id", "error_code", "user_id", "client_id", "module", "endpoint", "method", "status_code", "error_category",
                "error_message", "file_name", "line_number", "code_context", "stack_trace", "input_data", "suggested_fix", "is_resolved",
                "resolution_notes", "resolved_by", "resolved_at", "created_at"]
        };
        p.Redactions = values.EnumerateObject().Count(property => !known.Contains(property.Name));
        void Put(string key, NpgsqlDbType type, object? value) => p.Fields.Add(key, new(type, value));
        string Text(string key, int max, bool required = false)
        {
            var value = ReadString(values, key);
            if (value is null) { if (required) throw new RecordIssue("missing_field"); return ""; }
            if (value.EnumerateRunes().Count() > max || value.Any(char.IsControl)) throw new RecordIssue("unsupported_text");
            var safe = sanitizer.Text(value, max);
            if (safe != value) p.Redactions++;
            return safe;
        }
        string PublicText(string key, string fallback, int max)
        {
            if (manifest.PublicTextFields.Contains(record.Kind + "." + key)) return Text(key, max, required: true);
            if (HasValue(values, key)) p.Redactions++;
            return fallback;
        }
        DateTime? Time(string key, bool required)
        {
            var raw = ReadString(values, key);
            if (raw is null) { if (required) throw new RecordIssue("missing_time"); return null; }
            if (raw.Length > 64) throw new RecordIssue("invalid_time");
            DateTime instant;
            if (Regex.IsMatch(raw, "(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$", RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(50)))
            {
                if (!DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture, DateTimeStyles.None, out var offset))
                    throw new RecordIssue("invalid_time");
                instant = offset.UtcDateTime;
            }
            else
            {
                if (!manifest.NaiveTimeZones.TryGetValue(record.Kind + "." + key, out var zoneName)) throw new RecordIssue("missing_timezone");
                if (!DateTime.TryParseExact(raw, ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd HH:mm:ss.FFFFFFF", "yyyy-MM-ddTHH:mm:ss.FFFFFFF"],
                    CultureInfo.InvariantCulture, DateTimeStyles.None, out var local)) throw new RecordIssue("invalid_time");
                TimeZoneInfo zone;
                try { zone = TimeZoneInfo.FindSystemTimeZoneById(zoneName); }
                catch (Exception error) when (error is TimeZoneNotFoundException or InvalidTimeZoneException)
                    { throw new RecordIssue("invalid_timezone"); }
                if (zone.IsAmbiguousTime(local) || zone.IsInvalidTime(local)) throw new RecordIssue("ambiguous_time");
                instant = TimeZoneInfo.ConvertTimeToUtc(DateTime.SpecifyKind(local, DateTimeKind.Unspecified), zone);
            }
            if (instant.Ticks % 10 != 0) throw new RecordIssue("unsupported_time_precision");
            p.Times[key] = raw;
            return instant;
        }
        async Task<int?> Client(string key)
        {
            var source = ReadInt(values, key);
            if (source is null) return null;
            if (!manifest.ClientIds.TryGetValue(source.Value, out var mapped)) throw new RecordIssue("missing_client_map");
            await using var check = Command(connection, transaction, "SELECT EXISTS(SELECT 1 FROM public.clients WHERE id=$1)", (NpgsqlDbType.Integer, mapped));
            if (!(bool)(await check.ExecuteScalarAsync(token))!) throw new RecordIssue("missing_client");
            return mapped;
        }

        if (record.Kind == "error_codes")
        {
            Put("code", NpgsqlDbType.Text, sanitizer.Label(Text("code", 100, true)));
            Put("severity", NpgsqlDbType.Text, sanitizer.Label(Text("severity", 100, true)));
            Put("category", NpgsqlDbType.Text, sanitizer.Label(Text("category", 100, true)));
            Put("user_message", NpgsqlDbType.Text, PublicText("user_message", "Imported historical error.", 1000));
            Put("tech_description", NpgsqlDbType.Text, null);
            if (HasValue(values, "tech_description")) p.Redactions++;
            Put("suggested_action", NpgsqlDbType.Text, PublicText("suggested_action", "Contact the administrator.", 500));
            Put("is_active", NpgsqlDbType.Boolean, ReadBool(values, "is_active"));
            Put("created_at", NpgsqlDbType.TimestampTz, Time("created_at", true));
            return p;
        }
        var actorColumn = record.Kind == "error_log" ? "user_id" : "user";
        Put(actorColumn, NpgsqlDbType.Text, Text(actorColumn, 256, true));
        Put("actor_kind", NpgsqlDbType.Text, "imported_unknown");
        Put("actor_user_id", NpgsqlDbType.Integer, null);
        Put("correlation_id", NpgsqlDbType.Text, null);
        Put("event_id", NpgsqlDbType.Uuid, StableGuid(manifest.OriginId + "/" + record.Kind + "/" + record.SourceId));
        Put("operation_id", NpgsqlDbType.Uuid, StableGuid(manifest.OriginId + "/" + manifest.SnapshotId));
        Put("source_origin", NpgsqlDbType.Text, manifest.OriginId.ToString("D"));
        Put("source_record_id", NpgsqlDbType.Text, record.SourceId.ToString(CultureInfo.InvariantCulture));
        if (record.Kind == "audit_log")
        {
            Put("action", NpgsqlDbType.Text, sanitizer.Label(Text("action", 100, true)));
            Put("module", NpgsqlDbType.Text, sanitizer.Label(Text("module", 100, true)));
            var details = ParseJson(values, "details", allowInvalid: true);
            var safe = sanitizer.Metadata(details, 1000);
            if (HasValue(values, "details") && (details is null || safe != details.Value.GetRawText())) p.Redactions++;
            Put("details", NpgsqlDbType.Text, safe ?? (HasValue(values, "details") ? "{\"_omitted\":true}" : null));
            Put("client_id", NpgsqlDbType.Integer, await Client("client_id"));
            Put("timestamp", NpgsqlDbType.TimestampTz, Time("timestamp", true));
        }
        else if (record.Kind == "data_audit_log")
        {
            var sourceRow = ReadInt(values, "client_data_id") ?? throw new RecordIssue("missing_source_row");
            if (!manifest.CanonicalRows.TryGetValue(sourceRow, out var link)
                || !manifest.ClientIds.TryGetValue(link.SourceClientId, out var client)) throw new RecordIssue("missing_row_map");
            await using var check = Command(connection, transaction, "SELECT client_id FROM public.client_data WHERE id=$1 FOR KEY SHARE", (NpgsqlDbType.Integer, link.TargetId));
            if (await check.ExecuteScalarAsync(token) is not int actual || actual != client) throw new RecordIssue("foreign_source_row");
            await using (var clientCheck = Command(connection, transaction, "SELECT EXISTS(SELECT 1 FROM public.clients WHERE id=$1)", (NpgsqlDbType.Integer, client)))
                if (!(bool)(await clientCheck.ExecuteScalarAsync(token))!) throw new RecordIssue("missing_client");
            var action = sanitizer.Label(Text("action", 100, true));
            var old = ParseJson(values, "old_row_data"); var current = ParseJson(values, "new_row_data");
            var compact = sanitizer.HistoricalCompact(action.ToUpperInvariant(), old, current, manifest.ValueFields, record.PayloadMode == "full_rows");
            if ((old?.GetRawText() ?? "null") != (compact.Old ?? "null")) p.Redactions++;
            if ((current?.GetRawText() ?? "null") != (compact.New ?? "null")) p.Redactions++;
            Put("client_data_id", NpgsqlDbType.Integer, link.TargetId);
            Put("client_id", NpgsqlDbType.Integer, client);
            Put("action", NpgsqlDbType.Text, action);
            Put("old_row_data", NpgsqlDbType.Jsonb, compact.Old); Put("new_row_data", NpgsqlDbType.Jsonb, compact.New);
            Put("changed_at", NpgsqlDbType.TimestampTz, Time("changed_at", true));
        }
        else
        {
            int? registryId = null;
            var sourceCode = ReadInt(values, "error_code_id");
            var code = ReadString(values, "error_code");
            if (sourceCode is not null)
            {
                await using var mapping = Command(connection, transaction, """
                    SELECT m.target_id,m.target_hash,c.code FROM public.log_reconciliation_map m
                    JOIN public.error_codes c ON c.id=m.target_id
                    WHERE m.source_origin=$1 AND m.source_kind='error_codes' AND m.source_record_id=$2
                    """, (NpgsqlDbType.Uuid, manifest.OriginId), (NpgsqlDbType.Integer, sourceCode.Value));
                string expected; string mappedCode;
                await using (var reader = await mapping.ExecuteReaderAsync(token))
                {
                    if (!await reader.ReadAsync(token)) throw new RecordIssue("missing_registry_map");
                    registryId = reader.GetInt32(0); expected = reader.GetString(1); mappedCode = reader.GetString(2);
                }
                if (code is not null && code != mappedCode) throw new RecordIssue("registry_reference_conflict");
                if (await TargetHashAsync("error_codes", RegistryColumns, registryId.Value, connection, transaction, token) != expected)
                    throw new RecordIssue("registry_target_changed");
                code = mappedCode;
            }
            Put("error_code_id", NpgsqlDbType.Integer, registryId);
            Put("error_code", NpgsqlDbType.Text, code is null ? null : sanitizer.Label(code));
            Put("client_id", NpgsqlDbType.Integer, await Client("client_id"));
            Put("module", NpgsqlDbType.Text, sanitizer.Label(Text("module", 100, true)));
            Put("endpoint", NpgsqlDbType.Text, null); Put("method", NpgsqlDbType.Text, null);
            var status = ReadInt(values, "status_code");
            if (status is < 100 or > 599) throw new RecordIssue("invalid_status");
            Put("status_code", NpgsqlDbType.Integer, status);
            Put("error_category", NpgsqlDbType.Text, sanitizer.Label(Text("error_category", 100, true)));
            Put("error_message", NpgsqlDbType.Text, "Imported historical diagnostic; original retained in the protected source.");
            foreach (var key in new[] { "endpoint", "method", "error_message", "file_name", "line_number", "code_context", "stack_trace" })
                if (HasValue(values, key)) p.Redactions++;
            Put("file_name", NpgsqlDbType.Text, null); Put("line_number", NpgsqlDbType.Integer, null);
            Put("code_context", NpgsqlDbType.Text, null); Put("stack_trace", NpgsqlDbType.Text, null);
            var input = ParseJson(values, "input_data", allowInvalid: true);
            Put("input_data", NpgsqlDbType.Jsonb, sanitizer.Metadata(input));
            if (HasValue(values, "input_data")) p.Redactions++;
            Put("suggested_fix", NpgsqlDbType.Text, "Contact the administrator.");
            if (HasValue(values, "suggested_fix")) p.Redactions++;
            Put("is_resolved", NpgsqlDbType.Boolean, ReadBool(values, "is_resolved"));
            Put("resolved_by", NpgsqlDbType.Text, HasValue(values, "resolved_by") ? Text("resolved_by", 256) : null);
            Put("resolution_notes", NpgsqlDbType.Text, HasValue(values, "resolution_notes")
                ? PublicText("resolution_notes", "[omitted historical note]", 1000) : null);
            Put("resolved_at", NpgsqlDbType.TimestampTz, Time("resolved_at", false));
            Put("created_at", NpgsqlDbType.TimestampTz, Time("created_at", true));
        }
        Put("source_timestamp", NpgsqlDbType.Text, JsonSerializer.Serialize(p.Times));
        return p;
    }

    private static readonly string[] RegistryColumns = ["code", "severity", "category", "user_message", "tech_description", "suggested_action", "is_active", "created_at"];

    private static async Task<bool> RegistryMatchesAsync(Projection projection, int id, NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken token)
    {
        var fields = projection.Fields.Where(p => p.Key != "created_at").ToArray();
        var left = string.Join(',', fields.Select(p => "'" + p.Key + "',t.\"" + p.Key + "\""));
        var right = string.Join(',', fields.Select((p, i) => "'" + p.Key + "',$" + (i + 2)));
        await using var command = Command(connection, transaction,
            $"SELECT jsonb_build_object({left})=jsonb_build_object({right}) FROM public.error_codes t WHERE id=$1",
            new[] { (NpgsqlDbType.Integer, (object?)id) }.Concat(fields.Select(p => (p.Value.Type, p.Value.Value))).ToArray());
        return await command.ExecuteScalarAsync(token) is true;
    }

    private static async Task<string?> TargetHashAsync(string kind, IEnumerable<string> fields, int id,
        NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken token)
    {
        var columns = string.Join(',', new[] { "id" }.Concat(fields).Distinct().Order(StringComparer.Ordinal)
            .Select(k => "'" + k + "',t.\"" + k + "\""));
        await using var command = Command(connection, transaction,
            $"SELECT jsonb_build_object({columns})::text FROM public.{kind} t WHERE id=$1", (NpgsqlDbType.Integer, id));
        return await command.ExecuteScalarAsync(token) is string json ? Hash(json) : null;
    }

    private static string SourceHash(ReconciliationManifest manifest, LegacyLogRecord record) => Hash(JsonSerializer.Serialize(new
    {
        rule = 1, record.Kind, record.SourceId, record.PayloadMode,
        values = Canonical(record.Values), zones = manifest.NaiveTimeZones.OrderBy(p => p.Key, StringComparer.Ordinal).ToArray(),
        clients = manifest.ClientIds.OrderBy(p => p.Key).ToArray(), rows = manifest.CanonicalRows.OrderBy(p => p.Key).ToArray(),
        fields = manifest.ValueFields.Order(StringComparer.Ordinal).ToArray(), text = manifest.PublicTextFields.Order(StringComparer.Ordinal).ToArray()
    }));

    private static string Canonical(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object => "{" + string.Join(',', value.EnumerateObject().OrderBy(p => p.Name, StringComparer.Ordinal)
            .Select(p => JsonSerializer.Serialize(p.Name) + ":" + Canonical(p.Value))) + "}",
        JsonValueKind.Array => "[" + string.Join(',', value.EnumerateArray().Select(Canonical)) + "]",
        _ => value.GetRawText()
    };
    private static string Hash(string value) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(value)));
    private static Guid StableGuid(string value) => new(SHA256.HashData(Encoding.UTF8.GetBytes(value)).AsSpan(0, 16));
    private static bool HasValue(JsonElement values, string key) => values.TryGetProperty(key, out var v) && v.ValueKind != JsonValueKind.Null;
    private static string? ReadString(JsonElement values, string key) => !HasValue(values, key) ? null : values.GetProperty(key).GetString();
    private static int? ReadInt(JsonElement values, string key) => !HasValue(values, key) ? null : values.GetProperty(key).GetInt32() is var value && value > 0 ? value : throw new RecordIssue("invalid_identifier");
    private static bool ReadBool(JsonElement values, string key)
    {
        if (!HasValue(values, key)) throw new RecordIssue("missing_boolean");
        var value = values.GetProperty(key);
        return value.ValueKind switch
        {
            JsonValueKind.True => true, JsonValueKind.False => false,
            JsonValueKind.Number when value.TryGetInt32(out var n) && n is 0 or 1 => value.GetInt32() == 1,
            _ => throw new RecordIssue("invalid_boolean")
        };
    }
    private static JsonElement? ParseJson(JsonElement values, string key, bool allowInvalid = false)
    {
        if (!HasValue(values, key)) return null;
        var value = values.GetProperty(key);
        if (value.ValueKind != JsonValueKind.String) return value;
        try { using var parsed = JsonDocument.Parse(value.GetString()!, new() { MaxDepth = 16 }); return parsed.RootElement.Clone(); }
        catch (JsonException) when (allowInvalid) { return null; }
    }
    private static NpgsqlCommand Command(NpgsqlConnection connection, NpgsqlTransaction transaction, string sql,
        params (NpgsqlDbType Type, object? Value)[] parameters)
    {
        var command = new NpgsqlCommand(sql, connection, transaction);
        foreach (var p in parameters) command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = p.Type, Value = p.Value ?? DBNull.Value });
        return command;
    }
    private static async Task ExecuteAsync(NpgsqlConnection connection, NpgsqlTransaction transaction, string sql, CancellationToken token)
    {
        await using var command = Command(connection, transaction, sql);
        await command.ExecuteNonQueryAsync(token);
    }
}
