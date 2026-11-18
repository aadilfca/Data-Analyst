using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.Extensions.Options;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Selected-client lookup APIs used by the Verification screens.</summary>
public sealed class VerificationLookupService(PostgresTransactions transactions,
    VerificationSessionService sessions, VerificationStatusService status,
    IOptions<VerificationRuntimeOptions> options)
{
    private sealed record AccountSuggestion(string AccountCode, string AccountName, int? CategoryId,
        string? CategoryName, int Score);

    public Task<VerificationRuntimeResult> StatsAsync(VerifiedSession? session, CancellationToken ct = default) => Run(async token =>
    {
        int? clientId = session?.ClientId;
        if (clientId is not > 0) return Forbidden();
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await sessions.ResolveSelectedClientContextAsync(db, session, clientId.Value, inner);
            if (owner.Failure is not null) return owner.Failure;
            await using var command = db.CreateCommand("""
                SELECT COUNT(*)::int,
                    COUNT(*) FILTER (WHERE d.status='Pending')::int,
                    COUNT(*) FILTER (WHERE d.verified_at IS NOT NULL OR d.status IN ('Verified','Approved'))::int
                FROM public.verification_details d
                JOIN public.verification_sessions s ON s.id=d.session_id AND s.client_id=$1
                WHERE d.range_status='active' AND s.status<>'Cancelled'
                """);
            command.Parameters.AddWithValue(clientId.Value);
            await using var reader = await command.ExecuteReaderAsync(inner);
            if (!await reader.ReadAsync(inner)) return Success(new { total = 0, pending = 0, approved = 0, rejected = 0 });
            return Success(new { total = reader.GetInt32(0), pending = reader.GetInt32(1),
                approved = reader.GetInt32(2), rejected = 0 });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SupportingAsync(VerifiedSession? session, int clientId,
        string search, int templateId, int limit = 50, CancellationToken ct = default) => Run(async token =>
    {
        if (templateId < 1 || string.IsNullOrWhiteSpace(search) || search.Length > 200 || limit < 1)
            return Invalid("invalid_supporting_document_search");
        int boundedLimit = Math.Min(limit, 100);
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await sessions.ResolveSelectedClientContextAsync(db, session, clientId, inner);
            if (owner.Failure is not null) return owner.Failure;
            var rows = new List<object>();
            await using var command = db.CreateCommand("""
                SELECT id,row_data::text,extract_amount::text,extract_date::text
                FROM public.client_data
                WHERE client_id=$1 AND template_id=$2 AND is_deleted IS FALSE
                  AND EXISTS(SELECT 1 FROM jsonb_each_text(COALESCE(row_data,'{}'::jsonb)) kv
                    WHERE kv.value ILIKE $3)
                ORDER BY extract_date DESC NULLS LAST,id DESC LIMIT $4
                """);
            command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(templateId);
            command.Parameters.AddWithValue("%" + search + "%"); command.Parameters.AddWithValue(boundedLimit);
            await using var reader = await command.ExecuteReaderAsync(inner);
            while (await reader.ReadAsync(inner))
            {
                using var row = JsonDocument.Parse(reader.IsDBNull(1) ? "{}" : reader.GetString(1));
                rows.Add(new { id = reader.GetInt32(0), row_data = row.RootElement.Clone(),
                    extract_amount = DecimalOrNull(reader.IsDBNull(2) ? null : reader.GetString(2)),
                    extract_date = reader.IsDBNull(3) ? null : reader.GetString(3) });
            }
            return Success(new { supporting_documents = rows, count = rows.Count });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> AuditGuidanceAsync(VerifiedSession? session, string categoryName,
        CancellationToken ct = default) => Run(async token =>
    {
        if (string.IsNullOrWhiteSpace(categoryName) || categoryName.Length > 240) return Invalid("invalid_audit_guidance_category");
        int? clientId = session?.ClientId;
        if (clientId is not > 0) return Forbidden();
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await sessions.ResolveSelectedClientContextAsync(db, session, clientId.Value, inner);
            if (owner.Failure is not null) return owner.Failure;
            foreach (var category in ArrayProperty(owner.Context!.Rulebook, "categories"))
            {
                if (!string.Equals(Text(category, "name"), categoryName, StringComparison.Ordinal)) continue;
                return Success(new
                {
                    transaction_type = categoryName,
                    category_id = Int(category, "category_id"),
                    kb_overview = Text(category, "kb_overview"),
                    matrix_version = Int(category, "matrix_version"),
                    documents = ArrayProperty(category, "documents").ToArray(),
                    cross_checks = ArrayProperty(category, "cross_checks").ToArray(),
                    procedure_evidence = Property(category, "procedure_evidence"),
                    validation_rules = Property(category, "validation_rules")
                });
            }
            return NotFound("verification_audit_guidance_not_found");
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> CategoryForAccountAsync(VerifiedSession? session,
        int clientId, string accountCode, CancellationToken ct = default) => Run(async token =>
    {
        if (string.IsNullOrWhiteSpace(accountCode) || accountCode.Length > 128) return Invalid("invalid_account_code");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await sessions.ResolveSelectedClientContextAsync(db, session, clientId, inner);
            if (owner.Failure is not null) return owner.Failure;
            int? categoryId = null;
            await using (var live = db.CreateCommand("""
                SELECT a.category_id FROM public.trial_balance_uploads u
                JOIN public.trial_balance_accounts a ON a.upload_id=u.id AND a.account_code=$2
                WHERE u.client_id=$1 AND u.status='parsed'
                ORDER BY (u.period_end IS NULL),u.period_end DESC,u.uploaded_at DESC LIMIT 1
                """))
            {
                live.Parameters.AddWithValue(clientId); live.Parameters.AddWithValue(accountCode);
                var value = await live.ExecuteScalarAsync(inner);
                if (value is int id) categoryId = id;
            }
            if (categoryId is null)
            {
                await using var fallback = db.CreateCommand("SELECT category_id FROM public.account_category_assignments WHERE client_id=$1 AND account_code=$2 LIMIT 1");
                fallback.Parameters.AddWithValue(clientId); fallback.Parameters.AddWithValue(accountCode);
                var value = await fallback.ExecuteScalarAsync(inner);
                if (value is int id) categoryId = id;
            }
            if (categoryId is null) return Success(new { matched = false, category_id = (int?)null, category_name = (string?)null });
            await using var category = db.CreateCommand("SELECT definition->>'name' FROM public.platform_audit_rule_entities WHERE tenant_id=$1 AND entity_kind='categories' AND id=$2");
            category.Parameters.AddWithValue(owner.Context!.TenantId); category.Parameters.AddWithValue(categoryId.Value);
            string? name = await category.ExecuteScalarAsync(inner) as string;
            return name is null
                ? Success(new { matched = false, category_id = (int?)null, category_name = (string?)null })
                : Success(new { matched = true, category_id = categoryId, category_name = name });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> SuggestAccountHeadAsync(VerifiedSession? session,
        int clientId, string description, string? categoryName, CancellationToken ct = default) => Run(async token =>
    {
        if (string.IsNullOrWhiteSpace(description) || description.Length > 500) return Invalid("invalid_account_head_description");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            var owner = await sessions.ResolveSelectedClientContextAsync(db, session, clientId, inner);
            if (owner.Failure is not null) return owner.Failure;
            await using var upload = db.CreateCommand("""
                SELECT id FROM public.trial_balance_uploads WHERE client_id=$1 AND status='parsed'
                ORDER BY (period_end IS NULL),period_end DESC,uploaded_at DESC LIMIT 1
                """);
            upload.Parameters.AddWithValue(clientId);
            if (await upload.ExecuteScalarAsync(inner) is not int uploadId)
                return Success(new { matched = false, message = "No match — new head likely needed", suggestions = Array.Empty<object>() });
            var accounts = new List<AccountSuggestion>();
            await using (var query = db.CreateCommand("""
                SELECT a.account_code,a.account_name,a.category_id,c.definition->>'name'
                FROM public.trial_balance_accounts a
                LEFT JOIN public.platform_audit_rule_entities c ON c.tenant_id=$2 AND c.entity_kind='categories'
                    AND c.id=a.category_id AND c.is_active IS TRUE
                WHERE a.upload_id=$1 AND a.classification='leaf'
                """))
            {
                query.Parameters.AddWithValue(uploadId); query.Parameters.AddWithValue(owner.Context!.TenantId);
                await using var reader = await query.ExecuteReaderAsync(inner);
                while (await reader.ReadAsync(inner))
                {
                    string name = reader.IsDBNull(1) ? "" : reader.GetString(1);
                    if (name.Length == 0) continue;
                    accounts.Add(new(reader.IsDBNull(0) ? "" : reader.GetString(0), name,
                        reader.IsDBNull(2) ? null : reader.GetInt32(2), reader.IsDBNull(3) ? null : reader.GetString(3),
                        Similarity(description, name)));
                }
            }
            var top = accounts.Select(account =>
            {
                int score = account.Score;
                var reasons = new List<string>();
                if (!string.IsNullOrWhiteSpace(categoryName) && account.CategoryName is not null
                    && string.Equals(categoryName.Trim(), account.CategoryName.Trim(), StringComparison.OrdinalIgnoreCase))
                { score = Math.Min(100, score + 10); reasons.Add($"Category alignment: linked to Category '{account.CategoryName}'"); }
                string? keyword = MatchedKeyword(description, account.AccountName);
                if (keyword is not null) reasons.Add($"Matched keyword: '{keyword}'");
                reasons.Add(score >= 80 ? "High confidence match" : "Medium confidence match");
                return new { account_code = account.AccountCode, account_name = account.AccountName, score, reasons };
            }).OrderByDescending(item => item.score).ThenBy(item => item.account_code, StringComparer.Ordinal).Take(2)
              .Where(item => item.score >= 55).ToArray();
            return top.Length == 0
                ? Success(new { matched = false, message = "No match — new head likely needed", suggestions = Array.Empty<object>() })
                : Success(new { matched = true, suggestions = top });
        }, token);
    }, ct);

    public Task<VerificationRuntimeResult> LineStatusReasonAsync(VerifiedSession? session, int detailId,
        CancellationToken ct = default) => Run(async token =>
    {
        if (detailId < 1) return NotFound("verification_detail_not_found");
        return await transactions.ExecuteAsync(async (db, inner) =>
        {
            int? sessionId;
            await using (var find = db.CreateCommand("SELECT session_id FROM public.verification_details WHERE id=$1"))
            { find.Parameters.AddWithValue(detailId); sessionId = await find.ExecuteScalarAsync(inner) as int?; }
            if (sessionId is null) return NotFound("verification_detail_not_found");
            var owner = await sessions.ResolveReadContextAsync(db, session, sessionId.Value, inner);
            if (owner.Failure is not null) return owner.Failure;
            return await status.LineStatusReasonAsync(db, owner.Context!, detailId, inner);
        }, token);
    }, ct);

    private async Task<T> Run<T>(Func<CancellationToken, Task<T>> work, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(options.Value.TimeoutSeconds));
        return await work(timeout.Token);
    }

    private static int Similarity(string left, string right)
    {
        string a = Normalize(left), b = Normalize(right);
        if (a.Length == 0 || b.Length == 0) return 0;
        double lengthRatio = (double)Math.Max(a.Length, b.Length) / Math.Min(a.Length, b.Length);
        int scale = lengthRatio < 1.5 ? 95 : 60;
        double best = Ratio(a, b);
        double partial = PartialRatio(a, b);
        best = Math.Max(best, partial * (lengthRatio < 1.5 ? 1 : scale / 100d));
        string[] at = a.Split(' ', StringSplitOptions.RemoveEmptyEntries).Order(StringComparer.Ordinal).ToArray();
        string[] bt = b.Split(' ', StringSplitOptions.RemoveEmptyEntries).Order(StringComparer.Ordinal).ToArray();
        best = Math.Max(best, Ratio(string.Join(' ', at), string.Join(' ', bt)) * scale / 100d);
        var common = at.Intersect(bt, StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        if (common.Length > 0)
        {
            var commonText = string.Join(' ', common);
            var leftSet = string.Join(' ', common.Concat(at.Except(common, StringComparer.Ordinal).Order(StringComparer.Ordinal)));
            var rightSet = string.Join(' ', common.Concat(bt.Except(common, StringComparer.Ordinal).Order(StringComparer.Ordinal)));
            best = Math.Max(best, Math.Max(Ratio(commonText, leftSet), Ratio(commonText, rightSet)) * scale / 100d);
        }
        return (int)Math.Round(best, MidpointRounding.AwayFromZero);
    }
    private static int PartialRatio(string left, string right)
    {
        string shorter = left.Length <= right.Length ? left : right;
        string longer = left.Length <= right.Length ? right : left;
        if (shorter.Length == longer.Length) return Ratio(shorter, longer);
        int best = 0;
        for (int start = 0; start <= longer.Length - shorter.Length; start++)
            best = Math.Max(best, Ratio(shorter, longer.Substring(start, shorter.Length)));
        return best;
    }
    private static int Ratio(string a, string b)
    {
        int[] prior = Enumerable.Range(0, b.Length + 1).ToArray();
        for (int i = 1; i <= a.Length; i++)
        {
            var current = new int[b.Length + 1]; current[0] = i;
            for (int j = 1; j <= b.Length; j++) current[j] = Math.Min(Math.Min(current[j - 1] + 1,
                prior[j] + 1), prior[j - 1] + (a[i - 1] == b[j - 1] ? 0 : 2));
            prior = current;
        }
        return (int)Math.Round(100d * (a.Length + b.Length - prior[b.Length]) / (a.Length + b.Length), MidpointRounding.AwayFromZero);
    }
    private static string Normalize(string value) => string.Join(' ', new string(value.ToLowerInvariant()
        .Select(character => char.IsLetterOrDigit(character) ? character : ' ').ToArray())
        .Split(' ', StringSplitOptions.RemoveEmptyEntries));
    private static string? MatchedKeyword(string description, string accountName)
    {
        var stop = new HashSet<string>(["the", "and", "for", "from", "with", "this", "that", "account", "accounts"], StringComparer.Ordinal);
        var name = RegexWords(accountName).Where(token => token.Length >= 4 && !stop.Contains(token)).ToHashSet(StringComparer.Ordinal);
        return RegexWords(description).Where(token => token.Length >= 4 && !stop.Contains(token) && name.Contains(token))
            .OrderByDescending(token => token.Length).FirstOrDefault();
    }
    private static IEnumerable<string> RegexWords(string value) => System.Text.RegularExpressions.Regex
        .Matches(value.ToLowerInvariant(), "[a-zA-Z]+", System.Text.RegularExpressions.RegexOptions.CultureInvariant)
        .Select(match => match.Value);
    private static decimal? DecimalOrNull(string? value) => decimal.TryParse(value, NumberStyles.Float,
        CultureInfo.InvariantCulture, out decimal amount) ? amount : null;
    private static IEnumerable<JsonElement> ArrayProperty(JsonElement value, string key) =>
        Property(value, key).ValueKind == JsonValueKind.Array ? Property(value, key).EnumerateArray().ToArray() : [];
    private static JsonElement Property(JsonElement value, string key) => value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(key, out var result) ? result : default;
    private static string? Text(JsonElement value, string key) => Property(value, key).ValueKind == JsonValueKind.String
        ? Property(value, key).GetString() : null;
    private static int? Int(JsonElement value, string key) => Property(value, key).TryGetInt32(out int result) ? result : null;
    private static VerificationRuntimeResult Success<T>(T value) => new("success", JsonSerializer.SerializeToElement(value));
    private static VerificationRuntimeResult Invalid(string code) => new("invalid", Code: code);
    private static VerificationRuntimeResult NotFound(string code) => new("not_found", Code: code);
    private static VerificationRuntimeResult Forbidden() => new("forbidden", Code: "verification_client_access_denied");
}
