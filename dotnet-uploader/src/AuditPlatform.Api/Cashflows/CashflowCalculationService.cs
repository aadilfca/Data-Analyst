using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Reports;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.Cashflows;

/// <summary>Creates bounded Cashflow summaries from a configured, complete report snapshot.</summary>
internal sealed class CashflowCalculationService
{
    private sealed record Line(int Id, string Voucher, DateOnly Date, string AccountCode,
        string AccountName, string Detail, UploadExactAmount Debit, UploadExactAmount Credit,
        string Category, string PeriodKey, string PeriodLabel);

    private sealed class AmountByPeriod
    {
        public UploadExactAmount Value;
    }

    private static readonly UploadExactAmount Tolerance = UploadExactAmount.Parse("0.01");
    private static readonly string[] RequiredRoles = ["voucher_id", "date", "account_code", "debit", "credit"];

    public CashflowCalculationResult Calculate(ReportInputSnapshot snapshot, DateOnly start, DateOnly end,
        string groupBy = "month", string treasuryView = "combined", CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        if (snapshot.Scope.Kind != ReportInputKind.LedgerSource
            || snapshot.Scope.CategorySource != ReportAccountCategorySource.Cashflow)
            throw new FinancialStatementInputException("Cashflow inputs are unavailable for this report scope.");
        if (start > end || end == DateOnly.MaxValue) throw new FinancialStatementInputException("Choose a valid Cashflows reporting period.");
        string grouping = NormalizeGrouping(groupBy);
        string view = NormalizeView(treasuryView);
        Require(snapshot.ClientDataUploads, "configured Cashflows source");
        Require(snapshot.ClientData, "Cashflows transaction rows");
        Require(snapshot.CashflowSettings, "Cashflows settings");
        Require(snapshot.CashflowAccountAssignments, "Cashflows account assignments");
        Require(snapshot.CashflowRoleCategories, "Cashflows role categories");
        Require(snapshot.CashflowTreasuryAccounts, "Cash/Bank accounts");
        Require(snapshot.CashflowColumnMappings, "Cashflows column mappings");
        Require(snapshot.CategoryMappings, "Cashflows category assignments");
        Require(snapshot.Categories, "Cashflows categories");

        var settings = snapshot.CashflowSettings.Items.SingleOrDefault()
            ?? throw new FinancialStatementInputException("Cashflows settings are unavailable for this client.");
        var source = snapshot.ClientDataUploads.Items.SingleOrDefault()
            ?? throw new FinancialStatementInputException("The configured Cashflows source is unavailable for this client.");
        if (settings.SourceTemplateId is { } template && source.TemplateId != template
            || settings.SourceUploadId is { } upload && source.Id != upload)
            throw new FinancialStatementInputException("The captured Cashflows source no longer matches the configured source.");
        var mappings = snapshot.CashflowColumnMappings.Items.Where(row => row.TemplateId == source.TemplateId)
            .GroupBy(row => row.Role, StringComparer.Ordinal).ToDictionary(group => group.Key, group => group.Last().ColumnName,
                StringComparer.Ordinal);
        if (RequiredRoles.Any(role => !mappings.ContainsKey(role)))
            throw new FinancialStatementInputException("Map voucher, date, account code, debit and credit columns before generating Cashflows.");

        var accountTypes = snapshot.CashflowTreasuryAccounts.Items
            .GroupBy(row => row.AccountCode.Trim(), StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Last().TreasuryType.Equals("cash", StringComparison.OrdinalIgnoreCase) ? "Cash" : "Bank",
                StringComparer.Ordinal);
        if (accountTypes.Count == 0) throw new FinancialStatementInputException("Configure Cash/Bank accounts before generating Cashflows.");
        IReadOnlySet<string> selection = accountTypes.Keys.ToHashSet(StringComparer.Ordinal);
        if (snapshot.Scope.SelectedAccountCodes is { } selected)
        {
            if (selected.Count == 0 || selected.Any(code => !accountTypes.ContainsKey(code)))
                throw new FinancialStatementInputException("Select valid configured Cash/Bank accounts.");
            selection = selected.ToHashSet(StringComparer.Ordinal);
        }
        if (view == "cash" && !selection.Any(code => accountTypes[code] == "Cash")
            || view == "bank" && !selection.Any(code => accountTypes[code] == "Bank"))
            throw new FinancialStatementInputException("Select at least one account compatible with the requested Treasury view.");

        var categoryByCode = snapshot.CategoryMappings.Items
            .GroupBy(row => row.AccountCode.Trim(), StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Last().CategoryLabel ?? group.Last().CategoryKey ?? "",
                StringComparer.Ordinal);
        var categoryById = snapshot.Categories.Items.ToDictionary(row => row.Id);
        var categoryNamesByRole = snapshot.CashflowRoleCategories.Items
            .Where(row => categoryById.ContainsKey(row.CategoryId))
            .GroupBy(row => row.RoleKey, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.Select(row => categoryById[row.CategoryId].Label.Trim())
                .ToHashSet(StringComparer.OrdinalIgnoreCase), StringComparer.OrdinalIgnoreCase);
        var periodRows = Periods(start, end, grouping);
        var periods = periodRows.ToDictionary(row => row.Key, StringComparer.Ordinal);
        var parsed = new List<Line>(snapshot.ClientData.Items.Count);
        foreach (var row in snapshot.ClientData.Items)
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var json = JsonDocument.Parse(row.RowDataJson);
            if (json.RootElement.ValueKind != JsonValueKind.Object)
                throw new FinancialStatementInputException("A Cashflows source row is not a readable object.");
            string value(string role) => ReadColumn(json.RootElement, mappings[role]);
            string voucher = value("voucher_id").Trim();
            if (voucher.Length == 0) throw new FinancialStatementInputException("Cashflows source rows contain a blank voucher reference; review source identity.");
            DateOnly date = ParseDate(value("date"), row.ExtractDate);
            if (date < start || date > end) continue;
            string code = CanonicalCode(value("account_code"));
            if (code.Length == 0) throw new FinancialStatementInputException("Cashflows source rows contain a blank account code.");
            string key = PeriodKey(date, grouping);
            if (!periods.TryGetValue(key, out var period))
                throw new FinancialStatementInputException("A transaction date did not fall inside the requested reporting periods.");
            string accountName = mappings.TryGetValue("account_name", out _) ? value("account_name").Trim() : "";
            string detail = mappings.TryGetValue("voucher_detail", out _) ? value("voucher_detail").Trim() : "";
            parsed.Add(new(row.Id, voucher, date, code, accountName, detail,
                ParseAmount(value("debit")), ParseAmount(value("credit")),
                categoryByCode.GetValueOrDefault(code) is { Length: > 0 } category ? category : accountName.Length > 0 ? accountName : "Unmapped account",
                key, period.Label));
        }

        var selectedVouchers = parsed.Where(line => accountTypes.ContainsKey(line.AccountCode)
                && selection.Contains(line.AccountCode)
                && (view is "combined" or "split" || accountTypes[line.AccountCode].Equals(view, StringComparison.OrdinalIgnoreCase)))
            .Select(line => line.Voucher).ToHashSet(StringComparer.Ordinal);
        var working = parsed.Where(line => selectedVouchers.Contains(line.Voucher)).ToArray();
        var controls = new List<CashflowVoucherControl>();
        foreach (var group in working.GroupBy(line => (line.Voucher, line.PeriodKey)))
        {
            var treasury = group.Where(line => accountTypes.ContainsKey(line.AccountCode))
                .Aggregate(UploadExactAmount.Zero, (sum, line) => sum + line.Debit - line.Credit);
            var counter = group.Where(line => !accountTypes.ContainsKey(line.AccountCode))
                .Aggregate(UploadExactAmount.Zero, (sum, line) => sum + line.Credit - line.Debit);
            var variance = treasury - counter;
            controls.Add(new(group.Key.Voucher, group.Key.PeriodKey, treasury.ToFixedString(), counter.ToFixedString(),
                variance.ToFixedString(), variance.Abs().CompareTo(Tolerance) <= 0));
        }
        var badVouchers = controls.Where(row => !row.Balanced).Take(5).Select(row => row.VoucherId).ToArray();
        if (controls.Any(row => !row.Balanced))
            throw new FinancialStatementInputException("Cash/Bank selection contains unbalanced vouchers; examples: " + string.Join(", ", badVouchers));

        var currentTotals = BuildCategoryTotals(working, accountTypes, settings, categoryNamesByRole, periods, cancellationToken);
        var totals = currentTotals.Select(item => new CashflowCategoryTotal(item.Key,
            periodRows.ToDictionary(period => period.Key,
                period => item.Value.GetValueOrDefault(period.Key)?.Value.ToFixedString() ?? "0", StringComparer.Ordinal),
            item.Value.Values.Aggregate(UploadExactAmount.Zero, (sum, amount) => sum + amount.Value).ToFixedString())).ToArray();
        var periodTotals = periodRows.Select(period =>
        {
            UploadExactAmount periodReceipts = UploadExactAmount.Zero, periodPayments = UploadExactAmount.Zero;
            foreach (var category in currentTotals.Values)
            {
                var value = category.GetValueOrDefault(period.Key)?.Value ?? UploadExactAmount.Zero;
                if (value.CompareTo(UploadExactAmount.Zero) >= 0) periodReceipts += value;
                else periodPayments += UploadExactAmount.Zero - value;
            }
            return new CashflowPeriodTotal(period.Key, periodReceipts.ToFixedString(),
                periodPayments.ToFixedString(), (periodReceipts - periodPayments).ToFixedString());
        }).ToArray();
        var proofs = BuildProof(working, accountTypes, periodRows);
        UploadExactAmount receipts = UploadExactAmount.Zero, payments = UploadExactAmount.Zero;
        foreach (var item in currentTotals.Values)
            foreach (var amount in item.Values.Select(value => value.Value))
            {
                if (amount.CompareTo(UploadExactAmount.Zero) >= 0) receipts += amount;
                else payments += UploadExactAmount.Zero - amount;
            }
        var diagnostics = new List<string>();
        if (currentTotals.Keys.Contains("Unmapped account", StringComparer.OrdinalIgnoreCase))
            diagnostics.Add("Some counter-account rows have no Cashflow category assignment.");
        var postings = working.Where(line => accountTypes.ContainsKey(line.AccountCode))
            .OrderBy(line => line.Date).ThenBy(line => line.Id)
            .Select(line => new CashflowPostingEvidence(line.Id,line.Voucher,
                line.Date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),line.AccountCode,
                line.Debit.ToFixedString(),line.Credit.ToFixedString())).ToArray();
        return new(snapshot.Scope.ClientId, snapshot.Identity.Fingerprint, grouping, view,
            settings.ReceiptPresentation, periodRows, periodTotals, totals, controls, proofs, diagnostics,
            receipts.ToFixedString(), payments.ToFixedString(), postings);
    }

    private static SortedDictionary<string, Dictionary<string, AmountByPeriod>> BuildCategoryTotals(
        IReadOnlyList<Line> working, IReadOnlyDictionary<string, string> accountTypes,
        ReportCashflowSettings settings, IReadOnlyDictionary<string, HashSet<string>> roles,
        IReadOnlyDictionary<string, CashflowPeriod> periods, CancellationToken ct)
    {
        var result = new SortedDictionary<string, Dictionary<string, AmountByPeriod>>(StringComparer.OrdinalIgnoreCase);
        var roleNames = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (string role in new[] { "customer_receipt", "bank_charge", "withholding_tax", "other_deduction" })
            roleNames[role] = roles.GetValueOrDefault(role) ?? new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var voucher in working.GroupBy(line => line.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var contra = voucher.Where(line => !accountTypes.ContainsKey(line.AccountCode)).ToArray();
            var customer = contra.Where(line => roleNames["customer_receipt"].Contains(line.Category)).ToArray();
            bool onePeriod = voucher.Select(line => line.PeriodKey).Distinct(StringComparer.Ordinal).Count() == 1;
            UploadExactAmount gross = customer.Aggregate(UploadExactAmount.Zero,
                (sum, line) => sum + line.Credit - line.Debit);
            var deductions = contra.Where(line => roleNames["bank_charge"].Contains(line.Category)
                    || roleNames["withholding_tax"].Contains(line.Category)
                    || roleNames["other_deduction"].Contains(line.Category)).ToArray();
            UploadExactAmount deduction = deductions.Aggregate(UploadExactAmount.Zero,
                (sum, line) => sum + line.Debit - line.Credit);
            UploadExactAmount bankInflows = voucher.Where(line => accountTypes.GetValueOrDefault(line.AccountCode) == "Bank")
                .Aggregate(UploadExactAmount.Zero, (sum, line) => sum + Positive(line.Debit) + NegativeAsPositive(line.Credit));
            UploadExactAmount bankOutflows = voucher.Where(line => accountTypes.ContainsKey(line.AccountCode))
                .Aggregate(UploadExactAmount.Zero, (sum, line) => sum + Positive(line.Credit) + NegativeAsPositive(line.Debit));
            var netSettlement = bankInflows - bankOutflows;
            bool matched = customer.Select(line => line.AccountCode).Distinct(StringComparer.Ordinal).Count() == 1
                && voucher.Where(line => accountTypes.ContainsKey(line.AccountCode)).Select(line => line.AccountCode)
                    .Distinct(StringComparer.Ordinal).Count() == 1
                && onePeriod && (netSettlement - (gross - deduction)).Abs().CompareTo(Tolerance) <= 0
                && !contra.Any(line => !customer.Contains(line) && !deductions.Contains(line)
                    && (line.Debit.CompareTo(UploadExactAmount.Zero) != 0
                        || line.Credit.CompareTo(UploadExactAmount.Zero) != 0));
            UploadExactAmount settlement = settings.ReceiptPresentation == "net" && matched ? netSettlement : gross;
            var customerVoucher = customer.FirstOrDefault();
            foreach (var line in contra)
            {
                UploadExactAmount amount = line.Credit - line.Debit;
                if (customerVoucher is not null && settings.ReceiptPresentation == "net" && matched
                    && roleNames["customer_receipt"].Contains(line.Category))
                {
                    if (!ReferenceEquals(line, customerVoucher)) continue;
                    amount = settlement;
                }
                Add(result, line.Category, line.PeriodKey, amount);
            }
        }
        return result;
    }

    private static IReadOnlyList<CashflowMovementProof> BuildProof(IReadOnlyList<Line> rows,
        IReadOnlyDictionary<string, string> accountTypes, IReadOnlyList<CashflowPeriod> periods)
    {
        var list = new List<CashflowMovementProof>();
        foreach (var period in periods)
        {
            foreach (string type in new[] { "Cash", "Bank", "combined" })
            {
                var scoped = rows.Where(row => row.PeriodKey == period.Key && accountTypes.ContainsKey(row.AccountCode)
                    && (type == "combined" || accountTypes[row.AccountCode] == type)).ToArray();
                var movement = scoped.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
                var category = UploadExactAmount.Zero;
                var transfer = UploadExactAmount.Zero;
                var mixed = UploadExactAmount.Zero;
                var mixedVouchers = new HashSet<string>(StringComparer.Ordinal);
                foreach (var voucher in rows.Where(row => row.PeriodKey == period.Key).GroupBy(row => row.Voucher, StringComparer.Ordinal))
                {
                    var treasury = voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
                    if (treasury.Length == 0 || !treasury.Any(row => type == "combined" || accountTypes[row.AccountCode] == type)) continue;
                    var counter = voucher.Where(row => !accountTypes.ContainsKey(row.AccountCode)).ToArray();
                    var types = treasury.Select(row => accountTypes[row.AccountCode]).Distinct(StringComparer.Ordinal).ToArray();
                    var scopedTreasury = treasury.Where(row => type == "combined" || accountTypes[row.AccountCode] == type)
                        .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
                    if (counter.Length == 0) transfer += scopedTreasury;
                    else if (types.Length > 1)
                    {
                        mixedVouchers.Add(voucher.Key);
                        if (type != "combined") mixed += scopedTreasury;
                        else category += counter.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Credit - row.Debit);
                    }
                    else if (type == "combined" || types[0] == type)
                        category += counter.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Credit - row.Debit);
                }
                var explained = category + transfer + mixed;
                var variance = movement - explained;
                string status = mixedVouchers.Count > 0 ? "Review required"
                    : variance.Abs().CompareTo(Tolerance) <= 0 ? "Matched" : "Variance";
                string allocationStatus = mixedVouchers.Count > 0 ? "Review required" : "Proven";
                list.Add(new(period.Key, period.Label, type, movement.ToFixedString(), transfer.ToFixedString(),
                    mixed.ToFixedString(), explained.ToFixedString(), status, movement.ToFixedString(),
                    category.ToFixedString(), variance.ToFixedString(), mixedVouchers.Count, allocationStatus));
            }
        }
        return list;
    }

    private static void Add(SortedDictionary<string, Dictionary<string, AmountByPeriod>> data,
        string category, string period, UploadExactAmount amount)
    {
        if (!data.TryGetValue(category, out var periods)) data[category] = periods = new(StringComparer.Ordinal);
        if (!periods.TryGetValue(period, out var value)) periods[period] = value = new AmountByPeriod();
        value.Value += amount;
    }

    private static UploadExactAmount Positive(UploadExactAmount amount) => amount.CompareTo(UploadExactAmount.Zero) > 0 ? amount : UploadExactAmount.Zero;
    private static UploadExactAmount NegativeAsPositive(UploadExactAmount amount) => amount.CompareTo(UploadExactAmount.Zero) < 0 ? UploadExactAmount.Zero - amount : UploadExactAmount.Zero;
    private static void Require<T>(ReportInputDataset<T> set, string label)
    { if (!set.Requested || !set.Complete) throw new FinancialStatementInputException($"A complete {label} snapshot is required."); }

    private static string ReadColumn(JsonElement row, string column)
    {
        string key = NormalizeColumn(column);
        foreach (var property in row.EnumerateObject())
            if (NormalizeColumn(property.Name) == key)
                return property.Value.ValueKind switch
                {
                    JsonValueKind.String => property.Value.GetString() ?? "",
                    JsonValueKind.Number => property.Value.GetRawText(),
                    JsonValueKind.True => "true",
                    JsonValueKind.False => "false",
                    JsonValueKind.Null => "",
                    _ => property.Value.GetRawText()
                };
        return "";
    }

    private static string NormalizeColumn(string value) => new(value.Where(char.IsAsciiLetterOrDigit)
        .Select(char.ToLowerInvariant).ToArray());
    private static string CanonicalCode(string code) => code.Trim();
    private static UploadExactAmount ParseAmount(string value) => string.IsNullOrWhiteSpace(value)
        ? UploadExactAmount.Zero : UploadExactAmount.Parse(value);

    private static DateOnly ParseDate(string text, DateOnly? extracted)
    {
        if (DateOnly.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AllowWhiteSpaces, out var parsed)) return parsed;
        if (DateTime.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AllowWhiteSpaces, out var dateTime)) return DateOnly.FromDateTime(dateTime);
        if (double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var serial)
            && double.IsFinite(serial) && serial >= 0 && serial <= 2958465)
            return DateOnly.FromDateTime(new DateTime(1899, 12, 30).AddDays(serial));
        return extracted ?? throw new FinancialStatementInputException("A Cashflows transaction date is invalid or missing.");
    }

    private static string NormalizeGrouping(string value)
    {
        string grouping = value.Trim().ToLowerInvariant().Replace('-', '_');
        if (grouping is "annual") grouping = "year";
        if (grouping is "halfyear" or "half_yearly") grouping = "half_year";
        return grouping is "day" or "fortnight" or "month" or "quarter" or "half_year" or "year"
            ? grouping : throw new FinancialStatementInputException("Choose day, fortnight, month, quarter, half_year or year grouping.");
    }

    private static string NormalizeView(string value) => value.Trim().ToLowerInvariant() is "combined" or "cash" or "bank" or "split"
        ? value.Trim().ToLowerInvariant() : throw new FinancialStatementInputException("Choose combined, cash, bank or split Treasury view.");

    private static IReadOnlyList<CashflowPeriod> Periods(DateOnly start, DateOnly end, string grouping)
    {
        var result = new List<CashflowPeriod>();
        DateOnly cursor = start;
        while (cursor <= end)
        {
            DateOnly bucketStart, bucketEnd; string key, label;
            switch (grouping)
            {
                case "day": bucketStart = cursor; bucketEnd = cursor; key = cursor.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture); label = cursor.ToString("dd-MMM-yy", CultureInfo.InvariantCulture); break;
                case "fortnight":
                    bucketStart = new DateOnly(cursor.Year, cursor.Month, cursor.Day <= 15 ? 1 : 16);
                    bucketEnd = bucketStart.Day == 1 ? new DateOnly(cursor.Year, cursor.Month, 15) : new DateOnly(cursor.Year, cursor.Month, DateTime.DaysInMonth(cursor.Year, cursor.Month));
                    key = $"{cursor:yyyy-MM}-F{(bucketStart.Day == 1 ? 1 : 2)}";
                    label = $"F{(bucketStart.Day == 1 ? 1 : 2)} {cursor:MMM-yy}"; break;
                case "month":
                    bucketStart = new DateOnly(cursor.Year, cursor.Month, 1); bucketEnd = new DateOnly(cursor.Year, cursor.Month, DateTime.DaysInMonth(cursor.Year, cursor.Month));
                    key = cursor.ToString("yyyy-MM", CultureInfo.InvariantCulture); label = cursor.ToString("MMM-yy", CultureInfo.InvariantCulture); break;
                case "quarter":
                    int quarterStartMonth = ((cursor.Month - 1) / 3) * 3 + 1; bucketStart = new DateOnly(cursor.Year, quarterStartMonth, 1);
                    bucketEnd = bucketStart.AddMonths(3).AddDays(-1); key = $"{cursor.Year}-Q{((cursor.Month - 1) / 3) + 1}"; label = $"Q{((cursor.Month - 1) / 3) + 1} {cursor.Year}"; break;
                case "half_year":
                    int halfStartMonth = cursor.Month <= 6 ? 1 : 7; bucketStart = new DateOnly(cursor.Year, halfStartMonth, 1);
                    bucketEnd = bucketStart.AddMonths(6).AddDays(-1); key = $"{cursor.Year}-H{(halfStartMonth == 1 ? 1 : 2)}"; label = $"H{(halfStartMonth == 1 ? 1 : 2)} {cursor.Year}"; break;
                default: bucketStart = new DateOnly(cursor.Year, 1, 1); bucketEnd = new DateOnly(cursor.Year, 12, 31); key = cursor.Year.ToString(CultureInfo.InvariantCulture); label = key; break;
            }
            var clippedStart = bucketStart < start ? start : bucketStart;
            var clippedEnd = bucketEnd > end ? end : bucketEnd;
            result.Add(new(key, clippedStart.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                clippedEnd.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), label));
            cursor = clippedEnd.AddDays(1);
        }
        return result;
    }

    private static string PeriodKey(DateOnly date, string grouping) => grouping switch
    {
        "day" => date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
        "fortnight" => $"{date:yyyy-MM}-F{(date.Day <= 15 ? 1 : 2)}",
        "month" => date.ToString("yyyy-MM", CultureInfo.InvariantCulture),
        "quarter" => $"{date.Year}-Q{((date.Month - 1) / 3) + 1}",
        "half_year" => $"{date.Year}-H{(date.Month <= 6 ? 1 : 2)}",
        _ => date.Year.ToString(CultureInfo.InvariantCulture)
    };
}
