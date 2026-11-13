using System.Text;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.DataIntegrity;

public sealed class DatabaseSafeguardService(IOptions<DataIntegrityOptions> options)
{
    private static readonly Regex Tokens = new("'(?:''|[^'])*'|::|<>|[A-Za-z_][\\w]*|[^\\s()]", RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));

    // Normalize only PostgreSQL formatting; literal case and operators remain significant.
    private static IEnumerable<string> PredicateTokens(string? predicate)
    {
        var tokens = Tokens.Matches(predicate ?? "").Select(m => m.Value).ToArray();
        for (var i = 0; i < tokens.Length; i++)
        {
            if (tokens[i] == "::" && i + 1 < tokens.Length && tokens[i + 1].Equals("text", StringComparison.OrdinalIgnoreCase)) { i++; continue; }
            yield return tokens[i].StartsWith('\'') ? tokens[i] : tokens[i].ToLowerInvariant();
        }
    }

    public static bool Matches(IntegrityIndex index, IntegrityIndexRequirement required) =>
        index.TableName == required.Table && index.AccessMethod == "btree"
        && index.IsValid && index.IsReady && index.IsLive && index.StandardKeys
        && (required.Kind == "lookup" ? index.KeyColumns.Take(required.Columns.Length).SequenceEqual(required.Columns) : index.KeyColumns.SequenceEqual(required.Columns))
        && PredicateTokens(index.Predicate).SequenceEqual(PredicateTokens(required.Predicate))
        && (required.Kind == "lookup" || index.IsUnique && index.IsImmediate)
        && (required.Kind != "primary" || index.IsPrimary);

    public async Task<IReadOnlyList<IntegrityIndex>?> ReadCatalogAsync(PostgresTransactionContext db, CancellationToken ct)
    {
        return await DataIntegrityService.OptionalRead(db, async () =>
        {
            await using var command = db.CreateCommand("""
                SELECT t.relname::text, ix.relname::text, am.amname::text,
                    i.indisvalid,i.indisready,i.indislive,i.indisunique,i.indisprimary,i.indimmediate,
                    NOT EXISTS (
                        SELECT 1 FROM unnest(i.indclass,i.indcollation,i.indkey) WITH ORDINALITY k(opclass,collation_oid,attnum,pos)
                        LEFT JOIN pg_opclass oc ON oc.oid=k.opclass
                        LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
                        WHERE k.pos<=i.indnkeyatts AND (oc.opcdefault IS DISTINCT FROM TRUE OR a.attname IS NULL
                            OR k.collation_oid IS DISTINCT FROM a.attcollation)),
                    left(pg_get_expr(i.indpred,i.indrelid),8193),
                    ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(attnum,pos)
                        LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
                        WHERE k.pos<=i.indnkeyatts ORDER BY k.pos)
                FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
                JOIN pg_index i ON i.indrelid=t.oid JOIN pg_class ix ON ix.oid=i.indexrelid
                JOIN pg_am am ON am.oid=ix.relam
                WHERE n.nspname='public' AND t.relname=ANY($1)
                ORDER BY t.relname,ix.relname LIMIT $2
                """);
            command.Parameters.AddWithValue(IntegrityBaseline.Requirements.Select(r => r.Table).Distinct().ToArray());
            command.Parameters.AddWithValue(options.Value.MaximumCatalogEntries + 1);
            await using var reader = await command.ExecuteReaderAsync(ct);
            var rows = new List<IntegrityIndex>();
            long bytes = 0;
            while (await reader.ReadAsync(ct))
            {
                var predicate = reader.IsDBNull(10) ? null : reader.GetString(10);
                var keys = reader.GetFieldValue<string?[]>(11);
                bytes += Encoding.UTF8.GetByteCount(predicate ?? "") + keys.Sum(k => Encoding.UTF8.GetByteCount(k ?? "")) + 256;
                // A bounded catalog can be unavailable, but can never certify a partial list.
                if (rows.Count == options.Value.MaximumCatalogEntries || predicate?.Length > 8192 || bytes > options.Value.MaximumCatalogBytes)
                    return null;
                rows.Add(new(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetBoolean(3), reader.GetBoolean(4),
                    reader.GetBoolean(5), reader.GetBoolean(6), reader.GetBoolean(7), reader.GetBoolean(8), reader.GetBoolean(9), predicate, keys));
            }
            return (IReadOnlyList<IntegrityIndex>)rows;
        }, ct);
    }

    public static IntegritySafeguards Assess(IReadOnlyList<IntegrityModule> modules, IReadOnlyList<IntegrityIndex>? indexes)
    {
        var results = modules.Select(module =>
        {
            var required = IntegrityBaseline.Requirements.Where(r => r.Module == module.Module).ToArray();
            var missing = indexes is null ? [] : required.Where(r => !indexes.Any(i => Matches(i, r))).Select(r => r.Label).ToArray();
            var indexStatus = indexes is null ? "Not checked" : missing.Length > 0 ? "Needs attention" : "Passed";
            var identification = module.UnidentifiedRows is null ? "Not checked" : module.UnidentifiedRows > 0 ? "Needs attention" : "Passed";
            var duplicates = module.DuplicateGroups > 0 ? "Needs attention" : module.DuplicateStatus == "Clear" ? "Passed" : "Not checked";
            IntegrityCheck[] checks = [
                new("Required indexes", indexStatus, indexes is null ? "Index definitions could not be verified."
                    : missing.Length > 0 ? "Missing, different or unavailable: " + string.Join(", ", missing) : $"{required.Length} required structures are valid and ready."),
                new("Record identification", identification, module.UnidentifiedRows is null ? "Record identification could not be checked."
                    : module.UnidentifiedRows > 0 ? $"{module.UnidentifiedRows:N0} rows need identifying details."
                    : module.RowCount == 0 ? "No records to check." : $"All {module.RowCount:N0} records have the required identifying details."),
                new("Duplicate check", duplicates, module.DuplicateGroups > 0 ? $"{module.DuplicateGroups:N0} duplicate groups found."
                    : duplicates == "Not checked" ? "Some records could not be fully compared." : "No duplicate groups found within this module's check scope.")
            ];
            return new IntegritySafeguardModule(module.Module, module.RowCount, IntegrityBaseline.Status(checks.Select(c => c.Status)), checks, checks.Any(c => c.Status == "Not checked"));
        }).ToArray();
        return new(IntegrityBaseline.Status(results.Select(r => r.Status)), results.Any(r => r.Incomplete), results,
            "Index structures are shared across clients. Record identification and duplicate checks cover only the selected client and the three listed modules.",
            "This checks the required index baseline and identifying details. It does not inspect every physical index entry, verify all stored values, certify all upload prerequisites, or measure query speed or reclaimable storage.");
    }
}
