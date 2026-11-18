using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Data;
using Npgsql;

namespace AuditPlatform.Api.Verification;

/// <summary>Recomputes the Python-compatible Cleared/Query gates from the frozen rule snapshots.</summary>
public sealed class VerificationStatusService
{
    private sealed record EvidenceLink(int DocumentId, int ClientDataId, bool SignatureException);
    private static readonly string[] AccountAliases = ["Account Detail", "Account Details", "Account Code",
        "Account No", "Account Number", "Code", "Ledger Code", "AccountID", "Account Head", "Ledger Detail"];

    internal async Task<IReadOnlyDictionary<int, VerificationLineStatusResult>> RecomputeAsync(
        PostgresTransactionContext db, VerificationReviewContext owner, int[] detailIds, CancellationToken ct)
    {
        var result = new Dictionary<int, VerificationLineStatusResult>();
        foreach (int detailId in detailIds.Distinct())
        {
            var status = await RecomputeLineAsync(db, owner, detailId, ct);
            if (status is not null) result[detailId] = status;
        }
        return result;
    }

    private static async Task<VerificationLineStatusResult?> RecomputeLineAsync(PostgresTransactionContext db,
        VerificationReviewContext owner, int detailId, CancellationToken ct)
    {
        int clientDataId; string? transactionRef; int? frozenCategoryId; string? frozenCategoryName;
        string rowDataText; string? extractAmountText;
        await using (var command = db.CreateCommand("""
            SELECT vd.client_data_id,vd.transaction_ref,vd.category_id_applied,vd.category_name_applied,
                   cd.row_data::text,cd.extract_amount::text
            FROM public.verification_details vd
            JOIN public.verification_sessions vs ON vs.id=vd.session_id AND vs.client_id=$2
            JOIN public.client_data cd ON cd.id=vd.client_data_id AND cd.client_id=vs.client_id
            WHERE vd.id=$1 AND vd.range_status='active'
            """))
        {
            command.Parameters.AddWithValue(detailId); command.Parameters.AddWithValue(owner.ClientId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (!await reader.ReadAsync(ct)) return null;
            clientDataId = reader.GetInt32(0); transactionRef = reader.IsDBNull(1) ? null : reader.GetString(1);
            frozenCategoryId = reader.IsDBNull(2) ? null : reader.GetInt32(2);
            frozenCategoryName = reader.IsDBNull(3) ? null : reader.GetString(3);
            rowDataText = reader.GetString(4); extractAmountText = reader.IsDBNull(5) ? null : reader.GetString(5);
        }
        using var rowDocument = JsonDocument.Parse(rowDataText);
        JsonElement rowData = rowDocument.RootElement;
        string? accountCode = AccountCode(rowData);
        if (frozenCategoryId is null && string.IsNullOrWhiteSpace(accountCode)) return null;
        int? categoryId = frozenCategoryId;
        string? categoryName = frozenCategoryName;
        if (categoryId is null)
            (categoryId, categoryName) = await ResolveCategoryAsync(db, owner.TenantId, owner.ClientId, accountCode!, ct);
        if (categoryId is null || categoryName is null) return null;

        JsonElement profile = FindCategory(owner.Rulebook, categoryId.Value);
        if (profile.ValueKind != JsonValueKind.Object || (frozenCategoryId is null
            && (!profile.TryGetProperty("is_active", out var active) || !active.GetBoolean())))
            return null;
        var categorySnapshot = await ReadCategorySnapshotAsync(db, owner.TenantId, categoryId.Value, ct);
        var globalFingerprint = await ReadGlobalEvaluationFingerprintAsync(db, owner.TenantId, ct);
        if (categorySnapshot is null || globalFingerprint is null)
            throw new InvalidOperationException("The current Audit Rules snapshot is unavailable. Rebuild Audit Rules before saving verification work.");
        string appliedHash = AppliedRulesetHash(profile, categorySnapshot.EvaluationFingerprint, globalFingerprint);

        var siblingIds = await ReadSiblingIdsAsync(db, detailId, clientDataId, ct);
        var links = await ReadEvidenceLinksAsync(db, siblingIds, ct);
        // Python's evaluator treats every active Document compiled into the Category snapshot
        // as part of the evidence gate, regardless of the editor's display-only required flag.
        var categoryDocuments = ArrayProperty(profile, "documents").ToArray();
        bool documentsOk = await RequiredDocumentsCompleteAsync(db, siblingIds, categoryDocuments,
            ProcedureRoles(profile), Numeric(extractAmountText), ct);
        bool voucherChecksOk = await VoucherChecksCompleteAsync(db, owner.ClientId, transactionRef,
            owner.Rulebook, ct);
        bool signaturesOk = !links.Any(link => link.SignatureException);
        bool crossChecksOk = await CrossChecksPassAsync(db, rowData, links, siblingIds, profile, Numeric(extractAmountText), ct);
        bool accountHeadOk = !await HasLatestObservationTextAsync(db, siblingIds,
            "[System] Account Head Recommendation", -1, ct);
        bool correctnessOk = await HasLatestObservationTextAsync(db, siblingIds,
            "[System] Voucher Line Correctness", -2, ct, requiredText: "correct");
        string cleared = documentsOk && voucherChecksOk && signaturesOk && crossChecksOk && accountHeadOk && correctnessOk
            ? "Cleared" : "Query";
        await using (var update = db.CreateCommand("""
            UPDATE public.verification_details SET cleared_status=$1,matrix_version_applied=$2,
                category_id_applied=COALESCE(category_id_applied,$3),
                category_name_applied=COALESCE(category_name_applied,$4),applied_ruleset_hash=$5
            WHERE id=$6 AND range_status='active'
            """))
        {
            update.Parameters.AddWithValue(cleared);
            update.Parameters.AddWithValue((object?)Int(profile, "matrix_version") ?? DBNull.Value);
            update.Parameters.AddWithValue(categoryId.Value); update.Parameters.AddWithValue(categoryName);
            update.Parameters.AddWithValue(appliedHash); update.Parameters.AddWithValue(detailId);
            if (await update.ExecuteNonQueryAsync(ct) != 1) throw new InvalidOperationException("Verification line changed while status was being saved.");
        }
        return new(cleared, Int(profile, "matrix_version"), categoryId, categoryName, appliedHash);
    }

    private static async Task<(int? Id, string? Name)> ResolveCategoryAsync(PostgresTransactionContext db,
        int tenantId, int clientId, string accountCode, CancellationToken ct)
    {
        await using (var command = db.CreateCommand("""
            SELECT a.category_id,c.definition->>'name',c.definition->>'matrix_version'
            FROM public.trial_balance_uploads u
            JOIN public.trial_balance_accounts a ON a.upload_id=u.id AND a.account_code=$2
            JOIN public.platform_audit_rule_entities c ON c.id=a.category_id AND c.tenant_id=$3
              AND c.entity_kind='categories' AND c.is_active IS TRUE
            WHERE u.client_id=$1 AND u.status='parsed'
            ORDER BY (u.period_end IS NULL),u.period_end DESC,u.uploaded_at DESC LIMIT 1
            """))
        {
            command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(accountCode);
            // Tenant ownership is enforced by the selected client/session; the category entity is tenant-scoped.
            command.Parameters.AddWithValue(tenantId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct)) return (reader.GetInt32(0), reader.GetString(1));
        }
        await using (var command = db.CreateCommand("""
            SELECT a.category_id,c.definition->>'name'
            FROM public.account_category_assignments a
            JOIN public.platform_audit_rule_entities c ON c.id=a.category_id AND c.entity_kind='categories' AND c.is_active IS TRUE
            WHERE a.client_id=$1 AND a.account_code=$2 AND c.tenant_id=$3
            """))
        {
            command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(accountCode); command.Parameters.AddWithValue(tenantId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct)) return (reader.GetInt32(0), reader.IsDBNull(1) ? null : reader.GetString(1));
        }
        return (null, null);
    }

    private static async Task<int[]> ReadSiblingIdsAsync(PostgresTransactionContext db, int lineId, int clientDataId, CancellationToken ct)
    {
        var ids = new List<int>();
        await using var command = db.CreateCommand("SELECT id FROM public.verification_details WHERE client_data_id=$1 ORDER BY id");
        command.Parameters.AddWithValue(clientDataId); await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) ids.Add(reader.GetInt32(0));
        return ids.Count == 0 ? [lineId] : ids.ToArray();
    }

    private static async Task<List<EvidenceLink>> ReadEvidenceLinksAsync(PostgresTransactionContext db, int[] siblings, CancellationToken ct)
    {
        var result = new List<EvidenceLink>();
        if (siblings.Length == 0) return result;
        await using var command = db.CreateCommand("SELECT document_id,client_data_id,signature_exception FROM public.voucher_line_documents WHERE voucher_line_id=ANY($1) AND status='active'");
        command.Parameters.AddWithValue(siblings);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct)) result.Add(new(reader.GetInt32(0), reader.GetInt32(1), !reader.IsDBNull(2) && reader.GetBoolean(2)));
        return result;
    }

    private static async Task<bool> RequiredDocumentsCompleteAsync(PostgresTransactionContext db, int[] siblings,
        JsonElement[] documents, IReadOnlyDictionary<int, string[]> procedureRoles, double? voucherAmount, CancellationToken ct)
    {
        foreach (var document in documents)
        {
            int id = Int(document, "document_id") ?? 0;
            string type = String(document, "document_type") ?? "linked_template";
            int? template = Int(document, "template_id");
            if (type == "physical_bill")
            {
                if (!await PhysicalBillCompleteAsync(db, siblings, id, voucherAmount, ct)) return false;
            }
            else if (type == "form")
            {
                bool formComplete = await FormCompleteAsync(db, siblings, document, voucherAmount, ct);
                if (!formComplete) return false;
                if (procedureRoles.TryGetValue(id, out var formRoles) && formRoles.Length > 0
                    && !await RolesCompleteAsync(db, siblings, id, formRoles, ct)) return false;
            }
            else if (template is not null)
            {
                if (!await LinkedAsync(db, siblings, id, ct)) return false;
            }
            else
            {
                if (!procedureRoles.TryGetValue(id, out var roles) || roles.Length == 0
                    || !await RolesCompleteAsync(db, siblings, id, roles, ct)) return false;
            }
        }
        return true;
    }

    private static async Task<bool> LinkedAsync(PostgresTransactionContext db, int[] siblings, int documentId, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT EXISTS(SELECT 1 FROM public.voucher_line_documents WHERE voucher_line_id=ANY($1) AND document_id=$2 AND status='active')");
        command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(documentId);
        return (bool)(await command.ExecuteScalarAsync(ct) ?? false);
    }

    private static async Task<bool> RolesCompleteAsync(PostgresTransactionContext db, int[] siblings, int documentId, string[] roles, CancellationToken ct)
    {
        var latest = new Dictionary<string, (DateTimeOffset? At, bool Checked)>(StringComparer.Ordinal);
        await using var command = db.CreateCommand("SELECT role_name,is_checked,checked_at FROM public.voucher_line_procedure_checks WHERE voucher_line_id=ANY($1) AND document_id=$2");
        command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(documentId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            DateTimeOffset? at = reader.IsDBNull(2) ? null : new DateTimeOffset(reader.GetDateTime(2));
            string role = reader.GetString(0);
            if (!latest.TryGetValue(role, out var current) || Nullable.Compare(at, current.At) > 0)
                latest[role] = (at, reader.GetBoolean(1));
        }
        return roles.All(role => latest.TryGetValue(role, out var state) && state.Checked);
    }

    private static async Task<bool> PhysicalBillCompleteAsync(PostgresTransactionContext db, int[] siblings, int documentId, double? voucherAmount, CancellationToken ct)
    {
        if (voucherAmount is null) return false;
        double total = 0; int bills = 0;
        await using var command = db.CreateCommand("""
            WITH winning AS (
                SELECT voucher_line_id FROM public.voucher_line_physical_bills
                WHERE voucher_line_id=ANY($1) AND document_id=$2
                ORDER BY updated_at DESC,voucher_line_id DESC LIMIT 1
            )
            SELECT b.id,b.bill_no,b.bill_date,b.vendor_name,i.amount
            FROM public.voucher_line_physical_bills b LEFT JOIN public.voucher_line_physical_bill_items i ON i.bill_id=b.id
            WHERE b.voucher_line_id=(SELECT voucher_line_id FROM winning) AND b.document_id=$2
            ORDER BY b.sort_order,b.id,i.sort_order,i.id
            """);
        command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(documentId);
        await using var reader = await command.ExecuteReaderAsync(ct);
        int current = -1; bool headerOk = true; int items = 0;
        while (await reader.ReadAsync(ct))
        {
            int id = reader.GetInt32(0);
            if (id != current)
            {
                if (current >= 0 && (!headerOk || items == 0)) return false;
                current = id; bills++; items = 0;
                headerOk = !reader.IsDBNull(1) && !string.IsNullOrWhiteSpace(reader.GetString(1))
                    && !reader.IsDBNull(2) && !reader.IsDBNull(3) && !string.IsNullOrWhiteSpace(reader.GetString(3));
            }
            if (!reader.IsDBNull(4)) { total += Convert.ToDouble(reader.GetValue(4), CultureInfo.InvariantCulture); items++; }
            else return false;
        }
        if (bills == 0 || !headerOk || items == 0) return false;
        return Math.Abs(total - voucherAmount.Value) <= 0.005d;
    }

    private static async Task<bool> FormCompleteAsync(PostgresTransactionContext db, int[] siblings, JsonElement document,
        double? voucherAmount, CancellationToken ct)
    {
        var fields = ArrayProperty(document, "form_fields").ToArray();
        if (fields.Length == 0) return true;
        var fieldIds = fields.Select(field => Int(field, "id")).Where(id => id.HasValue).Select(id => id!.Value).ToHashSet();
        var rows = new List<(int LineId, DateTime Updated, int Group, string Scope, int Sort, int FieldId, string Type, string? Text, double? Number, DateTime? Date, bool HasValue)>();
        await using (var command = db.CreateCommand("""
            SELECT e.voucher_line_id,e.updated_at,e.bill_group_index,e.row_scope,e.sort_order,f.id,f.field_type,v.value_text,v.value_numeric,v.value_date
            FROM public.voucher_line_form_entries e JOIN public.voucher_line_form_values v ON v.entry_id=e.id
            JOIN public.audit_rule_document_fields f ON f.id=v.field_id
            WHERE e.voucher_line_id=ANY($1) AND e.document_id=$2 AND f.id=ANY($3)
            """))
        {
            command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(Int(document, "document_id") ?? 0);
            command.Parameters.AddWithValue(fieldIds.ToArray()); await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                string type = reader.GetString(6);
                string? text = reader.IsDBNull(7) ? null : reader.GetString(7);
                double? number = reader.IsDBNull(8) ? null : Convert.ToDouble(reader.GetValue(8), CultureInfo.InvariantCulture);
                DateTime? date = reader.IsDBNull(9) ? null : reader.GetDateTime(9);
                bool hasValue = type is "numeric" or "arithmetic_multiply" ? number.HasValue
                    : type == "date" ? date.HasValue : !string.IsNullOrWhiteSpace(text);
                rows.Add((reader.GetInt32(0), reader.GetDateTime(1), reader.GetInt32(2), reader.GetString(3),
                    reader.GetInt32(4), reader.GetInt32(5), type, text, number, date, hasValue));
            }
        }
        if (rows.Count == 0) return false;
        var latestEntry = rows.Max(row => (row.Updated, row.LineId));
        rows = rows.Where(row => (row.Updated, row.LineId) == latestEntry).ToList();
        var amountFields = fields.Where(field => String(field, "field_scope") == "item" && IsAmountField(field)).Select(field => Int(field, "id")!.Value).ToHashSet();
        if (amountFields.Count > 0)
        {
            var amounts = rows.Where(row => row.HasValue && row.Scope == "item" && amountFields.Contains(row.FieldId))
                .Select(row => row.Number ?? (double.TryParse(row.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) ? parsed : (double?)null)).ToArray();
            return amounts.Length > 0 && amounts.All(amount => amount.HasValue) && voucherAmount.HasValue
                && Math.Abs(amounts.Sum(amount => amount!.Value) - voucherAmount.Value) <= 0.005d;
        }
        foreach (var group in rows.GroupBy(row => row.Group))
        {
            var presentHeader = group.Where(row => row.HasValue && row.Scope == "header").Select(row => row.FieldId).ToHashSet();
            var requiredHeader = fields.Where(field => String(field, "field_scope") != "item").Select(field => Int(field, "id")!.Value).ToHashSet();
            if (!requiredHeader.IsSubsetOf(presentHeader)) return false;
            var itemFields = fields.Where(field => String(field, "field_scope") == "item").Select(field => Int(field, "id")!.Value).ToHashSet();
            var items = group.Where(row => row.HasValue && row.Scope == "item").ToArray();
            if (itemFields.Count > 0 && (items.Length == 0 || items.GroupBy(row => row.Sort).Any(item => !itemFields.IsSubsetOf(item.Select(row => row.FieldId).ToHashSet())))) return false;
        }
        return true;
    }

    private static bool IsAmountField(JsonElement field)
    {
        string label = new((String(field, "field_name") ?? "").ToLowerInvariant().Where(char.IsLetterOrDigit).ToArray());
        return label is "amount" or "billamount" or "totalamount" || String(field, "field_type") == "arithmetic_multiply" && label == "total";
    }

    private static async Task<bool> VoucherChecksCompleteAsync(PostgresTransactionContext db, int clientId, string? transactionRef,
        JsonElement rulebook, CancellationToken ct)
    {
        var checks = ArrayProperty(rulebook, "voucher_checks").Select(check => Int(check, "id")).Where(id => id.HasValue).Select(id => id!.Value).ToArray();
        if (checks.Length == 0) return true;
        if (string.IsNullOrWhiteSpace(transactionRef)) return false;
        await using var command = db.CreateCommand("SELECT check_id FROM public.voucher_check_status WHERE client_id=$1 AND transaction_ref=$2 AND state='done'");
        command.Parameters.AddWithValue(clientId); command.Parameters.AddWithValue(transactionRef);
        await using var reader = await command.ExecuteReaderAsync(ct); var done = new HashSet<int>();
        while (await reader.ReadAsync(ct)) done.Add(reader.GetInt32(0));
        return checks.All(done.Contains);
    }

    private static async Task<bool> HasLatestObservationTextAsync(PostgresTransactionContext db, int[] siblings,
        string systemName, int fallbackDocumentId, CancellationToken ct, string? requiredText = null)
    {
        await using var command = db.CreateCommand("""
            SELECT o.observation_text,o.updated_at FROM public.voucher_line_observations o
            LEFT JOIN public.audit_rule_documents d ON d.id=o.document_id
            WHERE o.voucher_line_id=ANY($1) AND (o.document_id=$2 OR d.name=$3)
            ORDER BY o.updated_at DESC,o.id DESC LIMIT 1
            """);
        command.Parameters.AddWithValue(siblings); command.Parameters.AddWithValue(fallbackDocumentId); command.Parameters.AddWithValue(systemName);
        var value = await command.ExecuteScalarAsync(ct);
        string? text = value is DBNull or null ? null : Convert.ToString(value, CultureInfo.InvariantCulture);
        return requiredText is null ? !string.IsNullOrWhiteSpace(text) : string.Equals(text?.Trim(), requiredText, StringComparison.OrdinalIgnoreCase);
    }

    private static async Task<bool> CrossChecksPassAsync(PostgresTransactionContext db, JsonElement rowData,
        IReadOnlyList<EvidenceLink> links, int[] siblingIds, JsonElement profile, double? voucherAmount, CancellationToken ct)
    {
        var linked = links.GroupBy(link => link.DocumentId).ToDictionary(group => group.Key, group => group.Select(item => item.ClientDataId).Distinct().ToArray());
        foreach (var rule in ArrayProperty(profile, "cross_checks"))
        {
            var docs = ArrayProperty(rule, "document_chain").OrderBy(doc => Int(doc, "sort_order") ?? 0).ToArray();
            if (docs.Length == 0) continue;
            var matrix = Prop(rule, "matrix");
            if (matrix.ValueKind == JsonValueKind.Object)
            {
                if (!await MatrixCrossCheckPassesAsync(db, rule, matrix, linked, siblingIds, voucherAmount, ct)) return false;
                continue;
            }
            string? field = String(rule, "compare_field");
            string comparison = String(rule, "comparison_type") ?? "all_equal";
            var chain = new List<(JsonElement Document, string Name, bool Linked, double? Value)>();
            foreach (var doc in docs)
            {
                int id = Int(doc, "document_id") ?? 0;
                bool manual = String(doc, "document_type") is "form" or "physical_bill";
                bool isLinked = linked.TryGetValue(id, out var dataIds) && dataIds.Length > 0;
                if (manual) isLinked = await ManualEvidenceExistsAsync(db, siblingIds, id, String(doc, "document_type")!, ct);
                double? sum = null;
                if (isLinked && field is not null)
                {
                    var rowValues = await ReadDocumentFieldValuesAsync(db, doc, id, manual ? siblingIds : dataIds!, field, ct);
                    if (rowValues.Count > 0) sum = rowValues.Sum();
                }
                chain.Add((doc, String(doc, "name") ?? "Document", isLinked, sum));
            }
            var comparable = chain.Where(item => item.Linked && item.Value.HasValue).ToArray();
            bool fail = false;
            if (comparable.Length >= 2)
            {
                const double epsilon = 0.005d;
                if (comparison is "All equal" or "Must be equal")
                {
                    double baseline = comparable[0].Value!.Value;
                    fail = comparable.Skip(1).Any(item => Math.Abs(item.Value!.Value - baseline) > epsilon);
                }
                else for (int index = 1; index < comparable.Length; index++)
                {
                    double first = comparable[index - 1].Value!.Value, next = comparable[index].Value!.Value;
                    fail |= comparison switch
                    {
                        "Sequential increasing" => next <= first + epsilon,
                        "Sequential decreasing" => next >= first - epsilon,
                        "May be less, never more" => next > first + epsilon,
                        "Within tolerance %" => Math.Abs(first) <= epsilon
                            ? Math.Abs(first - next) > epsilon
                            : Math.Abs(next - first) > Math.Abs(first) * 0.05d + epsilon,
                        _ => false
                    };
                }
            }
            var anchor = chain.LastOrDefault(item => item.Linked);
            if (Bool(rule, "compare_to_voucher_line") && anchor.Value.HasValue && voucherAmount.HasValue)
                fail |= Math.Abs(anchor.Value.Value - voucherAmount.Value) > 0.005d;
            if (fail && docs.Length > 0) return false;
        }
        return true;
    }

    private static async Task<List<double>> ReadEvidenceValuesAsync(PostgresTransactionContext db, int[] clientDataIds, string field, CancellationToken ct)
    {
        var result = new List<double>();
        if (clientDataIds.Length == 0) return result;
        await using var command = db.CreateCommand("SELECT row_data::text FROM public.client_data WHERE id=ANY($1)");
        command.Parameters.AddWithValue(clientDataIds); await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            using var document = JsonDocument.Parse(reader.GetString(0));
            if (TryField(document.RootElement, field, out var raw) && TryDouble(raw, out double amount)) result.Add(amount);
        }
        return result;
    }

    private static async Task<bool> ManualEvidenceExistsAsync(PostgresTransactionContext db, int[] siblingIds,
        int documentId, string documentType, CancellationToken ct)
    {
        string table = documentType == "form" ? "voucher_line_form_entries" : "voucher_line_physical_bills";
        await using var command = db.CreateCommand($"SELECT EXISTS(SELECT 1 FROM public.{table} WHERE voucher_line_id=ANY($1) AND document_id=$2)");
        command.Parameters.AddWithValue(siblingIds); command.Parameters.AddWithValue(documentId);
        return (bool)(await command.ExecuteScalarAsync(ct) ?? false);
    }

    private static async Task<List<double>> ReadDocumentFieldValuesAsync(PostgresTransactionContext db,
        JsonElement document, int documentId, int[] clientDataIds, string field, CancellationToken ct)
    {
        string type = String(document, "document_type") ?? "linked_template";
        string normalized = new(field.ToLowerInvariant().Where(char.IsLetterOrDigit).ToArray());
        if (type == "physical_bill")
        {
            if (normalized is not ("amount" or "billamount" or "totalamount" or "total")) return [];
            var values = new List<double>();
            await using var command = db.CreateCommand("""
                WITH winning AS (
                    SELECT voucher_line_id FROM public.voucher_line_physical_bills
                    WHERE voucher_line_id=ANY($1) AND document_id=$2
                    ORDER BY updated_at DESC,voucher_line_id DESC LIMIT 1
                )
                SELECT i.amount FROM public.voucher_line_physical_bills b
                JOIN public.voucher_line_physical_bill_items i ON i.bill_id=b.id
                WHERE b.voucher_line_id=(SELECT voucher_line_id FROM winning) AND b.document_id=$2
                ORDER BY b.sort_order,b.id,i.sort_order,i.id
                """);
            command.Parameters.AddWithValue(clientDataIds); command.Parameters.AddWithValue(documentId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct)) if (!reader.IsDBNull(0)) values.Add(Convert.ToDouble(reader.GetValue(0), CultureInfo.InvariantCulture));
            return values;
        }
        if (type == "form")
        {
            var values = new List<double>();
            await using var command = db.CreateCommand("""
                WITH winning AS (
                    SELECT e.voucher_line_id FROM public.voucher_line_form_entries e
                    WHERE e.voucher_line_id=ANY($1) AND e.document_id=$2
                    ORDER BY e.updated_at DESC,e.voucher_line_id DESC LIMIT 1
                )
                SELECT v.value_numeric,v.value_text
                FROM public.voucher_line_form_entries e
                JOIN public.voucher_line_form_values v ON v.entry_id=e.id
                JOIN public.audit_rule_document_fields f ON f.id=v.field_id AND f.document_id=e.document_id
                WHERE e.voucher_line_id=(SELECT voucher_line_id FROM winning)
                  AND e.document_id=$2 AND lower(f.field_name)=lower($3)
                ORDER BY e.updated_at,e.bill_group_index,e.row_scope,e.sort_order
                """);
            command.Parameters.AddWithValue(clientDataIds); command.Parameters.AddWithValue(documentId); command.Parameters.AddWithValue(field);
            await using var reader = await command.ExecuteReaderAsync(ct);
            while (await reader.ReadAsync(ct))
            {
                if (!reader.IsDBNull(0)) values.Add(Convert.ToDouble(reader.GetValue(0), CultureInfo.InvariantCulture));
                else if (!reader.IsDBNull(1) && CrossCheckNumeric(reader.GetString(1)) is double parsed) values.Add(parsed);
            }
            return values;
        }
        return await ReadEvidenceValuesAsync(db, clientDataIds, field, ct);
    }

    private static async Task<bool> MatrixCrossCheckPassesAsync(PostgresTransactionContext db, JsonElement rule, JsonElement matrix,
        IReadOnlyDictionary<int, int[]> linked, int[] siblingIds, double? voucherAmount, CancellationToken ct)
    {
        var docs = ArrayProperty(matrix, "documents").OrderBy(doc => Int(doc, "sort_order") ?? 0).ToArray();
        var chainValues = new Dictionary<int, double>();
        foreach (var field in ArrayProperty(matrix, "fields"))
        {
            var values = new List<double?>();
            JsonElement cells = Prop(field, "cells");
            int configuredCount = 0;
            foreach (var doc in docs)
            {
                int id = Int(doc, "document_id") ?? 0;
                if (!TryField(cells, id.ToString(CultureInfo.InvariantCulture), out var column) || column.ValueKind != JsonValueKind.String) continue;
                configuredCount++;
                bool manual = String(doc, "document_type") is "form" or "physical_bill";
                var ids = manual ? siblingIds : linked.GetValueOrDefault(id) ?? [];
                var data = await ReadDocumentFieldValuesAsync(db, doc, id, ids, column.GetString()!, ct);
                double? value = data.Count == 0 ? null : data.Sum();
                values.Add(value);
                if (value.HasValue && !chainValues.ContainsKey(id)) chainValues[id] = value.Value;
            }
            if (configuredCount < 2) continue;
            bool hasMissing = values.Any(value => !value.HasValue);
            if (hasMissing)
            {
                string missingBehavior = String(field, "missing_data_behavior") ?? "not_configured";
                if (missingBehavior is not ("not_configured" or "not_linked") && Bool(field, "blocking")) return false;
                continue;
            }
            string op = String(field, "operator") ?? "=";
            double tolerance = TryField(field, "tolerance", out var toleranceJson) && TryDouble(toleranceJson, out var parsedTolerance) ? parsedTolerance : 0;
            bool fail = values.Zip(values.Skip(1), (left, right) => left.HasValue && right.HasValue && (op switch
            {
                "=" => Math.Abs(left.Value-right.Value) > tolerance + 0.005d,
                "!=" => Math.Abs(left.Value-right.Value) <= tolerance + 0.005d,
                ">=" => left.Value < right.Value - tolerance - 0.005d,
                "<=" => left.Value > right.Value + tolerance + 0.005d,
                ">" => left.Value <= right.Value + tolerance + 0.005d,
                "<" => left.Value >= right.Value - tolerance - 0.005d,
                _ => false
            })).Any(value => value);
            if (fail && Bool(field, "blocking")) return false;
        }
        if (Bool(rule, "compare_to_voucher_line") && voucherAmount.HasValue)
        {
            var anchorValue = docs.Reverse().Select(doc => Int(doc, "document_id"))
                .Where(id => id.HasValue && chainValues.ContainsKey(id.Value))
                .Select(id => chainValues[id!.Value]).Cast<double?>().FirstOrDefault();
            if (anchorValue.HasValue && Math.Abs(anchorValue.Value-voucherAmount.Value) > 0.005d) return false;
        }
        return true;
    }

    private static JsonElement FindCategory(JsonElement rulebook, int categoryId)
    {
        foreach (var category in ArrayProperty(rulebook, "categories")) if (Int(category, "category_id") == categoryId) return category;
        return default;
    }
    private sealed record Snapshot(string EvaluationFingerprint);
    private static async Task<Snapshot?> ReadCategorySnapshotAsync(PostgresTransactionContext db, int tenant, int category, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT evaluation_fingerprint FROM public.platform_audit_rule_category_snapshots WHERE tenant_id=$1 AND category_id=$2 AND config_version<=(SELECT config_version FROM public.platform_audit_rule_state WHERE tenant_id=$1) ORDER BY config_version DESC LIMIT 1");
        command.Parameters.AddWithValue(tenant); command.Parameters.AddWithValue(category); var value = await command.ExecuteScalarAsync(ct);
        return value is string fingerprint ? new(fingerprint.Trim()) : null;
    }
    private static async Task<string?> ReadGlobalEvaluationFingerprintAsync(PostgresTransactionContext db, int tenant, CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT evaluation_fingerprint FROM public.platform_audit_rule_snapshots WHERE tenant_id=$1 AND config_version=(SELECT config_version FROM public.platform_audit_rule_state WHERE tenant_id=$1) AND schema_version=2 AND evaluator_version=1");
        command.Parameters.AddWithValue(tenant); var value = await command.ExecuteScalarAsync(ct);
        return value is string fingerprint ? fingerprint.Trim() : null;
    }
    private static string AppliedRulesetHash(JsonElement category, string categoryFingerprint, string globalFingerprint)
    {
        var roles = new List<JsonElement>();
        foreach (var check in ArrayProperty(Prop(category, "procedure_evidence"), "checks"))
        {
            var selectorFields = new[] { "document_id", "condition", "role", "signature", "selected_detail", "requires_client_resolution" };
            var selectorNode = new Dictionary<string, JsonElement?>();
            foreach (string key in selectorFields) selectorNode[key] = TryField(check, key, out var field) ? field : null;
            var selector = JsonSerializer.SerializeToElement(selectorNode, CanonicalOptions);
            string role = String(check, "role_name") ?? "";
            roles.Add(JsonSerializer.SerializeToElement(new { document_id = Int(check, "document_id"), selector = Canonical(selector), role_name = role }, CanonicalOptions));
        }
        var payload = JsonSerializer.SerializeToElement(new { evaluator_version = 1, category_evaluation_fingerprint = categoryFingerprint,
            global_evaluation_fingerprint = globalFingerprint, role_resolution = roles.OrderBy(item => Canonical(item), StringComparer.Ordinal).ToArray() }, CanonicalOptions);
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(Canonical(payload))));
    }
    private static readonly JsonSerializerOptions CanonicalOptions = new()
    { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    private static string Canonical(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object => "{" + string.Join(",", value.EnumerateObject().OrderBy(p => p.Name, StringComparer.Ordinal).Select(p => JsonSerializer.Serialize(p.Name, CanonicalOptions) + ":" + Canonical(p.Value))) + "}",
        JsonValueKind.Array => "[" + string.Join(",", value.EnumerateArray().Select(Canonical)) + "]",
        JsonValueKind.String => JsonSerializer.Serialize(value.GetString(), CanonicalOptions), JsonValueKind.Number => value.GetRawText(),
        JsonValueKind.True => "true", JsonValueKind.False => "false", JsonValueKind.Null => "null", _ => "null"
    };
    private static string? AccountCode(JsonElement row)
    {
        foreach (string key in AccountAliases) if (TryField(row, key, out var value) && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))
        { var text = value.ValueKind == JsonValueKind.String ? value.GetString() : value.GetRawText(); if (!string.IsNullOrWhiteSpace(text)) return text; }
        return null;
    }
    private static bool TryField(JsonElement row, string name, out JsonElement value)
    {
        if (row.ValueKind == JsonValueKind.Object && row.TryGetProperty(name, out value)) return true;
        if (row.ValueKind == JsonValueKind.Object)
            foreach (var property in row.EnumerateObject()) if (string.Equals(property.Name, name, StringComparison.OrdinalIgnoreCase)) { value = property.Value; return true; }
        value = default; return false;
    }
    private static bool TryDouble(JsonElement value, out double result)
    {
        if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out result)) return double.IsFinite(result);
        double? parsed = CrossCheckNumeric(value.ValueKind == JsonValueKind.String ? value.GetString() : value.GetRawText());
        result = parsed ?? 0d; return parsed.HasValue;
    }
    private static double? Numeric(string? value) => CrossCheckNumeric(value);
    private static double? CrossCheckNumeric(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        string text = value.Replace(",", "", StringComparison.Ordinal).Trim();
        var match = Regex.Match(text, @"^[^\d\-]*?(-?(?:\d+(?:\.\d+)?|\.\d+))\s*[^\d]*$", RegexOptions.CultureInvariant);
        return match.Success && double.TryParse(match.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out double number)
            && double.IsFinite(number) ? number : null;
    }
    private static bool Bool(JsonElement value, string key, bool fallback = false) => TryField(value, key, out var item) ? item.ValueKind == JsonValueKind.True || item.ValueKind != JsonValueKind.False && fallback : fallback;
    private static int? Int(JsonElement value, string key) => TryField(value, key, out var item) && item.TryGetInt32(out int result) ? result : null;
    private static string? String(JsonElement value, string key) => TryField(value, key, out var item) && item.ValueKind == JsonValueKind.String ? item.GetString() : null;
    private static JsonElement Prop(JsonElement value, string key) => TryField(value, key, out var item) ? item : default;
    private static IEnumerable<JsonElement> ArrayProperty(JsonElement value, string key) { var item = Prop(value, key); return item.ValueKind == JsonValueKind.Array ? item.EnumerateArray().ToArray() : []; }
    private static IReadOnlyDictionary<int, string[]> ProcedureRoles(JsonElement category)
    {
        var result = new Dictionary<int, string[]>();
        var configured = ArrayProperty(Prop(category, "procedure_evidence"), "checks").ToArray();
        foreach (var document in ArrayProperty(category, "documents"))
        {
            int? documentId = Int(document, "document_id");
            if (documentId is null) continue;
            var roles = ArrayProperty(document, "authority_roles").Select(role => String(role, "role_name"))
                .Where(role => !string.IsNullOrWhiteSpace(role)).Cast<string>().Distinct(StringComparer.Ordinal).ToArray();
            if (configured.Length == 0) { result[documentId.Value] = roles; continue; }
            var resolved = new List<string>();
            foreach (var check in configured.Where(check => Int(check, "document_id") == documentId))
            {
                string? explicitRole = String(check, "role") ?? String(check, "role_name") ?? String(check, "signature");
                string detail = (String(check, "selected_detail") ?? String(check, "sub_value") ?? String(check, "detail") ?? "").Trim();
                string condition = (String(check, "condition") ?? "").ToLowerInvariant();
                IEnumerable<string> candidates = roles;
                if (condition == "prepared_by") candidates = roles.Where(role => role.Contains("prepar", StringComparison.OrdinalIgnoreCase));
                else if (condition == "checked_by") candidates = roles.Where(role => role.Contains("check", StringComparison.OrdinalIgnoreCase));
                else if (condition == "approved_by") candidates = roles.Where(role => role.Contains("approv", StringComparison.OrdinalIgnoreCase));
                if (!string.IsNullOrWhiteSpace(explicitRole)) candidates = candidates.Where(role => string.Equals(role, explicitRole, StringComparison.OrdinalIgnoreCase));
                else if (!string.IsNullOrWhiteSpace(detail)) candidates = candidates.Where(role => string.Equals(role, detail, StringComparison.OrdinalIgnoreCase));
                foreach (string role in candidates) if (!resolved.Contains(role, StringComparer.Ordinal)) resolved.Add(role);
            }
            result[documentId.Value] = resolved.ToArray();
        }
        return result;
    }
}
