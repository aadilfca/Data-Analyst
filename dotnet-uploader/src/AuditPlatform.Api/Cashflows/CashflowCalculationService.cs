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
        Require(snapshot.AccountAssignments, "Cashflows verification workstream assignments");

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
        var voucherContext = new List<Line>();
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
            string code = CanonicalCode(value("account_code"));
            if (code.Length == 0) throw new FinancialStatementInputException("Cashflows source rows contain a blank account code.");
            string accountName = mappings.TryGetValue("account_name", out _) ? value("account_name").Trim() : "";
            string detail = mappings.TryGetValue("voucher_detail", out _) ? value("voucher_detail").Trim() : "";
            var debit=ParseAmount(value("debit"));var credit=ParseAmount(value("credit"));
            string category=categoryByCode.GetValueOrDefault(code) is { Length: > 0 } assigned
                ? assigned : accountName.Length > 0 ? accountName : "Unmapped account";
            if (date < start || date > end)
            {
                if(snapshot.Scope.IncludeVoucherContext)
                    voucherContext.Add(new(row.Id,voucher,date,code,accountName,detail,debit,credit,category,"","Outside range"));
                continue;
            }
            string key = PeriodKey(date, grouping);
            if (!periods.TryGetValue(key, out var period))
                throw new FinancialStatementInputException("A transaction date did not fall inside the requested reporting periods.");
            parsed.Add(new(row.Id, voucher, date, code, accountName, detail,debit,credit,category,key,period.Label));
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

        // A selected Treasury account does not by itself prove that every
        // counter-account in a mixed voucher belongs to that account. Project
        // categories only for complete selected vouchers, or where one
        // same-day Treasury funding/receiving hub is uniquely identifiable.
        var projection = ProjectOwnedCategories(working, accountTypes, selection, view, categoryNamesByRole);
        var categoryWorking = projection.Lines;
        var customerDetails = BuildCustomerDetails(categoryWorking, accountTypes, settings.ReceiptPresentation,
            categoryNamesByRole, cancellationToken);
        var currentTotals = BuildCategoryTotals(categoryWorking, accountTypes, settings, categoryNamesByRole,
            customerDetails, cancellationToken);
        var ledgerRollups = BuildLedgerRollups(categoryWorking, accountTypes, settings, customerDetails, cancellationToken);
        var vendorCodes = snapshot.AccountAssignments.Items
            .Where(row => row.VerificationWorkstream is "local_vendor" or "import_vendor")
            .Select(row => row.AccountCode.Trim()).ToHashSet(StringComparer.Ordinal);
        var vendorDetails = BuildVendorDetails(categoryWorking, vendorCodes, cancellationToken);
        var vendorTotals = BuildVendorTotals(vendorDetails, false);
        var vendorPeriodTotals = BuildVendorTotals(vendorDetails, true);
        var transferDetails = BuildTransferDetails(working, accountTypes, settings.MatchInternalTransfers,
            cancellationToken);
        var treasuryCategoryTotals = BuildTreasuryCategoryTotals(categoryWorking, accountTypes, settings.ReceiptPresentation,
            customerDetails, periodRows, categoryNamesByRole, cancellationToken);
        var accountCustomerDetails = BuildCustomerAccountDetails(categoryWorking, customerDetails, accountTypes,
            categoryNamesByRole, cancellationToken);
        var customerTotals = BuildCustomerPaymentSummaries(accountCustomerDetails);
        var customerPeriodTotals = BuildCustomerPaymentPeriodTotals(accountCustomerDetails, customerDetails,
            currentTotals, categoryNamesByRole, periodRows);
        var transferReconciliation = BuildTransferReconciliation(transferDetails);
        var treasuryReconciliation = BuildTreasuryReconciliation(currentTotals, treasuryCategoryTotals);
        var customerReconciliation = BuildCustomerPaymentReconciliation(customerDetails, customerTotals,
            currentTotals, categoryNamesByRole);
        var customerVouchers = categoryWorking.Where(line => !accountTypes.ContainsKey(line.AccountCode)
                && categoryNamesByRole.GetValueOrDefault("customer_receipt")?.Contains(line.Category) == true)
            .Select(line => line.Voucher).ToHashSet(StringComparer.Ordinal);
        var deductionCategories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string role in new[] { "bank_charge", "withholding_tax", "other_deduction" })
            if (categoryNamesByRole.TryGetValue(role, out var names)) deductionCategories.UnionWith(names);
        var receiptCategories = categoryNamesByRole.GetValueOrDefault("customer_receipt")
            ?? new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var reviewLines = categoryWorking.Where(line => !accountTypes.ContainsKey(line.AccountCode)
                && (string.Equals(line.Category, "Unmapped account", StringComparison.OrdinalIgnoreCase)
                    || customerVouchers.Contains(line.Voucher) && !receiptCategories.Contains(line.Category)
                        && !deductionCategories.Contains(line.Category)
                        && (line.Credit - line.Debit).CompareTo(Tolerance) > 0))
            .OrderBy(line => line.Date).ThenBy(line => line.Id)
            .Select(line => new CashflowReviewLine(line.Voucher, line.Date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                line.PeriodKey, line.PeriodLabel, line.AccountCode, line.AccountName, line.Debit.ToFixedString(),
                line.Credit.ToFixedString(), (line.Credit - line.Debit).ToFixedString(),
                string.Equals(line.Category, "Unmapped account", StringComparison.OrdinalIgnoreCase)
                    ? "No Cashflow category assignment" : "Unassigned customer settlement contra requires review"))
            .ToArray();
        var periodMetrics = BuildPeriodMetrics(categoryWorking, transferDetails, reviewLines, categoryNamesByRole, periodRows);
        var deductions = BuildDeductionDetails(categoryWorking, customerDetails, accountTypes,
            categoryNamesByRole, settings.ReceiptPresentation);
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
        var proofs = BuildProof(working, categoryWorking, accountTypes, periodRows, selection, view,
            projection.UnallocatedVouchers, projection.MixedVouchers, projection.TreasuryHubs);
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
        var postings = working.Where(line => accountTypes.ContainsKey(line.AccountCode) && selection.Contains(line.AccountCode)
                && (view is "combined" or "split" || accountTypes[line.AccountCode].Equals(view, StringComparison.OrdinalIgnoreCase)))
            .OrderBy(line => line.Date).ThenBy(line => line.Id)
            .Select(line => new CashflowPostingEvidence(line.Id,line.Voucher,
                line.Date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),line.AccountCode,
                line.Debit.ToFixedString(),line.Credit.ToFixedString())).ToArray();
        var ownedCategoryIds = categoryWorking.Where(line => !accountTypes.ContainsKey(line.AccountCode))
            .Select(line => line.Id).ToHashSet();
        var ownedVoucherIds=categoryWorking.Where(line=>!accountTypes.ContainsKey(line.AccountCode))
            .Select(line=>line.Voucher).ToHashSet(StringComparer.Ordinal);
        var categoryTreasuryTypes=categoryWorking.Where(line=>accountTypes.ContainsKey(line.AccountCode))
            .GroupBy(line=>line.Voucher,StringComparer.Ordinal).ToDictionary(group=>group.Key,
                group=>{var types=group.Select(line=>accountTypes[line.AccountCode]).Distinct(StringComparer.Ordinal).ToArray();
                    return types.Length==1?types[0]:"Mixed";},StringComparer.Ordinal);
        var reconciliationRows=working.Concat(voucherContext.Where(line=>selectedVouchers.Contains(line.Voucher)))
            .DistinctBy(line=>line.Id).Select(line => new CashflowReconciliationLine(line.Id,line.Voucher,
                line.Date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),line.PeriodKey,line.AccountCode,
                line.AccountName,line.Category,line.Debit.ToFixedString(),line.Credit.ToFixedString(),
                accountTypes.ContainsKey(line.AccountCode),ownedCategoryIds.Contains(line.Id)
                    || !accountTypes.ContainsKey(line.AccountCode)&&ownedVoucherIds.Contains(line.Voucher),
                line.PeriodKey.Length>0,categoryTreasuryTypes.GetValueOrDefault(line.Voucher,""))).ToArray();
        return new(snapshot.Scope.ClientId, snapshot.Identity.Fingerprint, grouping, view,
            settings.ReceiptPresentation, periodRows, periodTotals, totals, controls, proofs, diagnostics,
            receipts.ToFixedString(), payments.ToFixedString(), postings)
        {
            CustomerPaymentDetails = customerDetails,
            VendorPaymentDetails = vendorDetails,
            VendorPaymentVendorTotals = vendorTotals,
            VendorPaymentPeriodTotals = vendorPeriodTotals,
            CustomerPaymentCustomerOptions = customerTotals.Select(row => new CashflowCustomerPaymentOption(
                row.CustomerAccountCode, row.CustomerAccountName)).ToArray(),
            TransferDetails = transferDetails,
            TreasuryCategoryTotals = treasuryCategoryTotals,
            CategoriesNeedingReview = reviewLines,
            SelectionReviewDetails = projection.Reviews,
            SelectedVoucherCount = working.Select(line => line.Voucher).Distinct(StringComparer.Ordinal).Count(),
            SourceLineCount = working.Length,
            InternalTransferCount = transferDetails.Count,
            CategoryReviewCount = reviewLines.Length,
            DeductionCount = deductions.Count,
            DeductionDetails = deductions,
            CustomerPaymentCustomerTotals = customerTotals,
            CustomerPaymentAccountPeriodTotals = CustomerTotalsByPeriod(accountCustomerDetails),
            CustomerPaymentPeriodTotals = customerPeriodTotals,
            CustomerPaymentReconciliation = customerReconciliation,
            TransferReconciliation = transferReconciliation,
            TreasuryReconciliation = treasuryReconciliation,
            PeriodMetrics = periodMetrics,
            LedgerRollups = ledgerRollups,
            ReconciliationLines = reconciliationRows
        };
    }

    private static IReadOnlyList<CashflowLedgerRollup> BuildLedgerRollups(IReadOnlyList<Line> working,
        IReadOnlyDictionary<string, string> accountTypes, ReportCashflowSettings settings,
        IReadOnlyList<CashflowCustomerPaymentDetail> customerDetails, CancellationToken ct)
    {
        static string Join(IEnumerable<string> values) => string.Join(" / ", values.Where(value => !string.IsNullOrWhiteSpace(value))
            .Distinct(StringComparer.Ordinal));
        var detailsByVoucher = customerDetails.ToDictionary(row => row.VoucherId, StringComparer.Ordinal);
        var detailRows = new List<(string Category, string Period, string Label, string Code,
            string Name, string Treasury, UploadExactAmount Amount)>();
        foreach (var voucher in working.GroupBy(row => row.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var treasury = voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
            var counter = voucher.Where(row => !accountTypes.ContainsKey(row.AccountCode)).ToArray();
            if (counter.Length == 0) continue;
            string treasuryType = Join(treasury.Select(row => accountTypes.GetValueOrDefault(row.AccountCode, "Unknown")));
            if (treasuryType.Length == 0) treasuryType = "Unknown";
            detailsByVoucher.TryGetValue(voucher.Key, out var customer);
            bool netted = settings.ReceiptPresentation == "net" && customer?.Status == "Matched"
                && counter.Any(row => row.AccountCode.Equals(customer.CustomerAccountCode, StringComparison.Ordinal));
            if (netted && customer is not null)
            {
                var row = counter.First(line => line.AccountCode.Equals(customer.CustomerAccountCode, StringComparison.Ordinal));
                detailRows.Add((row.Category, row.PeriodKey, row.PeriodLabel,
                    "__customer_payment__", "Customer payment (net bank receipt)", treasuryType,
                    UploadExactAmount.Parse(customer.NetBankSettlement ?? customer.CashflowAmount)));
                continue;
            }
            foreach (var row in counter)
                detailRows.Add((row.Category, row.PeriodKey, row.PeriodLabel, row.AccountCode,
                    row.AccountName, treasuryType, row.Credit - row.Debit));
        }
        return detailRows.GroupBy(row => (row.Category, row.Period, row.Label, row.Code, row.Name, row.Treasury))
            .Select(group => new CashflowLedgerRollup(group.Key.Category, group.Key.Period, group.Key.Label,
                group.Key.Code, group.Key.Name, group.Key.Treasury,
                group.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Amount).ToFixedString(), group.Count()))
            .OrderBy(row => row.PeriodKey, StringComparer.Ordinal).ThenBy(row => row.Category, StringComparer.OrdinalIgnoreCase)
            .ThenBy(row => row.AccountCode, StringComparer.Ordinal).ToArray();
    }

    private sealed record CategoryProjection(IReadOnlyList<Line> Lines,
        IReadOnlySet<string> UnallocatedVouchers, IReadOnlySet<string> MixedVouchers,
        IReadOnlyDictionary<string, string> TreasuryHubs,IReadOnlyList<CashflowSelectionReview> Reviews);

    private static CategoryProjection ProjectOwnedCategories(IReadOnlyList<Line> rows,
        IReadOnlyDictionary<string, string> accountTypes, IReadOnlySet<string> selected,
        string view, IReadOnlyDictionary<string, HashSet<string>> roles)
    {
        var effective = selected.Where(code => view is "combined" or "split"
                || accountTypes.GetValueOrDefault(code)?.Equals(view, StringComparison.OrdinalIgnoreCase) == true)
            .ToHashSet(StringComparer.Ordinal);
        var customerCategories = roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase);
        var customerVouchers = rows.Where(row => !accountTypes.ContainsKey(row.AccountCode)
                && customerCategories.Contains(row.Category))
            .Select(row => row.Voucher).ToHashSet(StringComparer.Ordinal);
        var result = new List<Line>();
        var unallocated = new HashSet<string>(StringComparer.Ordinal);
        var mixed = new HashSet<string>(StringComparer.Ordinal);
        var hubs = new Dictionary<string, string>(StringComparer.Ordinal);
        var reviews = new List<CashflowSelectionReview>();
        foreach (var voucher in rows.GroupBy(row => row.Voucher, StringComparer.Ordinal))
        {
            var treasury = voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
            if (treasury.Length == 0) continue;
            var codes = treasury.Select(row => row.AccountCode).ToHashSet(StringComparer.Ordinal);
            bool counterparts = voucher.Any(row => !accountTypes.ContainsKey(row.AccountCode));
            bool complete = codes.IsSubsetOf(effective);
            string? hub = null;
            if (!complete && counterparts && !customerVouchers.Contains(voucher.Key)
                && treasury.Select(row => row.Date).Distinct().Count() == 1)
            {
                var incoming = treasury.Where(row => Inflow(row).CompareTo(Tolerance) > 0)
                    .Select(row => row.AccountCode).ToHashSet(StringComparer.Ordinal);
                var outgoing = treasury.Where(row => Outflow(row).CompareTo(Tolerance) > 0)
                    .Select(row => row.AccountCode).ToHashSet(StringComparer.Ordinal);
                if (!incoming.Overlaps(outgoing))
                {
                    var incomingHubs = incoming.Where(codes.Contains).ToArray();
                    var outgoingHubs = outgoing.Where(codes.Contains).ToArray();
                    if (incomingHubs.Length == 1 && incoming.Count == 1) hub = incomingHubs[0];
                    else if (outgoingHubs.Length == 1 && outgoing.Count == 1) hub = outgoingHubs[0];
                }
            }

            bool owns = counterparts && (complete || hub is not null && effective.Contains(hub));
            if (!counterparts) continue; // Pure Treasury transfers carry no category allocation.
            if (hub is not null) hubs[voucher.Key] = hub;
            if (!owns)
            {
                unallocated.Add(voucher.Key);
                foreach(var block in voucher.Where(row=>effective.Contains(row.AccountCode)).GroupBy(row=>row.PeriodKey,StringComparer.Ordinal))
                {
                    var inflow=block.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+Positive(row.Debit)+NegativeAsPositive(row.Credit));
                    var outflow=block.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+Positive(row.Credit)+NegativeAsPositive(row.Debit));
                    var first=block.First();
                    string reason=customerVouchers.Contains(voucher.Key)
                        ? "Customer settlement spans selected and unselected Treasury accounts"
                        : "Counterpart allocation is not proven for selected accounts";
                    reviews.Add(new(voucher.Key,first.Date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),
                        block.Key,first.PeriodLabel,reason,inflow.ToFixedString(),outflow.ToFixedString()));
                }
                continue;
            }
            string[] ownedCodes = complete ? codes.ToArray() : [hub!];
            if (ownedCodes.Select(code => accountTypes[code]).Distinct(StringComparer.Ordinal).Count() > 1)
                mixed.Add(voucher.Key);
            result.AddRange(voucher.Where(row => !accountTypes.ContainsKey(row.AccountCode)));
            result.AddRange(treasury.Where(row => ownedCodes.Contains(row.AccountCode, StringComparer.Ordinal)));
        }
        return new(result, unallocated, mixed, hubs,reviews);

        static UploadExactAmount Inflow(Line row) => Positive(row.Debit) + NegativeAsPositive(row.Credit);
        static UploadExactAmount Outflow(Line row) => Positive(row.Credit) + NegativeAsPositive(row.Debit);
    }

    private static SortedDictionary<string, Dictionary<string, AmountByPeriod>> BuildCategoryTotals(
        IReadOnlyList<Line> working, IReadOnlyDictionary<string, string> accountTypes,
        ReportCashflowSettings settings, IReadOnlyDictionary<string, HashSet<string>> roles,
        IReadOnlyList<CashflowCustomerPaymentDetail> customerDetails, CancellationToken ct)
    {
        var result = new SortedDictionary<string, Dictionary<string, AmountByPeriod>>(StringComparer.OrdinalIgnoreCase);
        var roleNames = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (string role in new[] { "customer_receipt", "bank_charge", "withholding_tax", "other_deduction" })
            roleNames[role] = roles.GetValueOrDefault(role) ?? new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var customerByVoucher = customerDetails.ToDictionary(row => row.VoucherId, StringComparer.Ordinal);
        foreach (var voucher in working.GroupBy(line => line.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var contra = voucher.Where(line => !accountTypes.ContainsKey(line.AccountCode)).ToArray();
            var customer = contra.Where(line => roleNames["customer_receipt"].Contains(line.Category)).ToArray();
            customerByVoucher.TryGetValue(voucher.Key, out var detail);
            var firstCustomer = customer.FirstOrDefault();
            foreach (var line in contra)
            {
                UploadExactAmount amount = line.Credit - line.Debit;
                if (firstCustomer is not null && detail?.Status == "Matched"
                    && settings.ReceiptPresentation == "net"
                    && roleNames["customer_receipt"].Contains(line.Category))
                {
                    if (!ReferenceEquals(line, firstCustomer)) continue;
                    amount = UploadExactAmount.Parse(detail.CashflowAmount);
                }
                Add(result, line.Category, line.PeriodKey, amount);
            }
        }
        return result;
    }

    private static IReadOnlyList<CashflowMovementProof> BuildProof(IReadOnlyList<Line> rows,
        IReadOnlyList<Line> categoryRows, IReadOnlyDictionary<string, string> accountTypes,
        IReadOnlyList<CashflowPeriod> periods, IReadOnlySet<string> selected, string view,
        IReadOnlySet<string> unallocatedVouchers, IReadOnlySet<string> selectedMixedVouchers,
        IReadOnlyDictionary<string, string> treasuryHubs)
    {
        var list = new List<CashflowMovementProof>();
        var effective = selected.Where(code => view is "combined" or "split"
                || accountTypes.GetValueOrDefault(code)?.Equals(view, StringComparison.OrdinalIgnoreCase) == true)
            .ToHashSet(StringComparer.Ordinal);
        foreach (var period in periods)
        {
            foreach (string type in new[] { "Cash", "Bank", "combined" })
            {
                var scoped = rows.Where(row => row.PeriodKey == period.Key && effective.Contains(row.AccountCode)
                    && (type == "combined" || accountTypes[row.AccountCode] == type)).ToArray();
                var movement = scoped.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
                var category = UploadExactAmount.Zero;
                var transfer = UploadExactAmount.Zero;
                var mixed = UploadExactAmount.Zero;
                var unallocatedCategory = UploadExactAmount.Zero;
                var mixedVouchers = new HashSet<string>(StringComparer.Ordinal);
                foreach (var voucher in rows.Where(row => row.PeriodKey == period.Key).GroupBy(row => row.Voucher, StringComparer.Ordinal))
                {
                    var treasury = voucher.Where(row => effective.Contains(row.AccountCode)).ToArray();
                    if (treasury.Length == 0 || !treasury.Any(row => type == "combined" || accountTypes[row.AccountCode] == type)) continue;
                    var scopedTreasury = treasury.Where(row => type == "combined" || accountTypes[row.AccountCode] == type)
                        .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
                    bool hasCounterpart = voucher.Any(row => !accountTypes.ContainsKey(row.AccountCode));
                    if (!hasCounterpart) transfer += scopedTreasury;
                    else if (unallocatedVouchers.Contains(voucher.Key))
                    {
                        mixedVouchers.Add(voucher.Key);
                        mixed += scopedTreasury;
                    }
                    else
                    {
                        if (treasuryHubs.TryGetValue(voucher.Key, out var hub))
                        {
                            foreach (var leg in voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)
                                && row.AccountCode != hub && effective.Contains(row.AccountCode)
                                && (type == "combined" || accountTypes[row.AccountCode] == type)))
                                transfer += leg.Debit - leg.Credit;
                            if (effective.Contains(hub) && (type == "combined" || accountTypes[hub] == type))
                                transfer -= voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)
                                        && row.AccountCode != hub)
                                    .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
                        }
                        var projected = categoryRows.Where(row => row.Voucher == voucher.Key && row.PeriodKey == period.Key).ToArray();
                        var projectedTreasury = projected.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
                        var projectedCounter = projected.Where(row => !accountTypes.ContainsKey(row.AccountCode)).ToArray();
                        if (projectedCounter.Length == 0) continue;
                        var projectedTypes = projectedTreasury.Select(row => accountTypes[row.AccountCode])
                            .Distinct(StringComparer.Ordinal).ToArray();
                        var amount = projectedCounter.Aggregate(UploadExactAmount.Zero,
                            (sum, row) => sum + row.Credit - row.Debit);
                        if (selectedMixedVouchers.Contains(voucher.Key) || projectedTypes.Length > 1)
                        {
                            mixedVouchers.Add(voucher.Key);
                            if (type == "combined") category += amount;
                            else if (projectedTypes.Contains(type, StringComparer.Ordinal)) mixed += scopedTreasury;
                        }
                        else if (type == "combined" || projectedTypes.Contains(type, StringComparer.Ordinal)) category += amount;
                    }
                    if (mixedVouchers.Contains(voucher.Key) && type == "combined")
                        unallocatedCategory += categoryRows.Where(row => row.Voucher == voucher.Key && row.PeriodKey == period.Key
                                && !accountTypes.ContainsKey(row.AccountCode))
                            .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Credit - row.Debit);
                }
                var explained = category + transfer + mixed;
                var variance = movement - explained;
                string status = mixedVouchers.Count > 0 ? "Review required"
                    : variance.Abs().CompareTo(Tolerance) <= 0 ? "Matched" : "Variance";
                string allocationStatus = mixedVouchers.Count > 0 ? "Review required" : "Proven";
                list.Add(new(period.Key, period.Label, type, movement.ToFixedString(), transfer.ToFixedString(),
                    mixed.ToFixedString(), explained.ToFixedString(), status, movement.ToFixedString(),
                    category.ToFixedString(), unallocatedCategory.ToFixedString(), variance.ToFixedString(), mixedVouchers.Count, allocationStatus));
            }
        }
        return list;
    }

    private static IReadOnlyList<CashflowCustomerPaymentDetail> BuildCustomerDetails(
        IReadOnlyList<Line> rows, IReadOnlyDictionary<string, string> accountTypes, string presentation,
        IReadOnlyDictionary<string, HashSet<string>> roles, CancellationToken ct)
    {
        var customerCategories = roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase);
        var chargeCategories = roles.GetValueOrDefault("bank_charge") ?? new(StringComparer.OrdinalIgnoreCase);
        var withholdingCategories = roles.GetValueOrDefault("withholding_tax") ?? new(StringComparer.OrdinalIgnoreCase);
        var otherCategories = roles.GetValueOrDefault("other_deduction") ?? new(StringComparer.OrdinalIgnoreCase);
        var results = new List<CashflowCustomerPaymentDetail>();
        foreach (var voucher in rows.GroupBy(row => row.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var all = voucher.ToArray();
            var customers = all.Where(row => !accountTypes.ContainsKey(row.AccountCode)
                && customerCategories.Contains(row.Category)
                && (row.Debit.Abs().CompareTo(Tolerance) > 0 || row.Credit.Abs().CompareTo(Tolerance) > 0)).ToArray();
            if (customers.Length == 0) continue;
            var treasury = all.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
            if (treasury.Length == 0) continue;
            var deductions = all.Where(row => !accountTypes.ContainsKey(row.AccountCode)
                && (chargeCategories.Contains(row.Category) || withholdingCategories.Contains(row.Category)
                    || otherCategories.Contains(row.Category))).ToArray();
            UploadExactAmount Sum(IEnumerable<Line> source, Func<Line, UploadExactAmount> selector) =>
                source.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + selector(row));
            UploadExactAmount receipts = Sum(customers, row => Positive(row.Credit) + NegativeAsPositive(row.Debit));
            UploadExactAmount reversals = Sum(customers, row => Positive(row.Debit) + NegativeAsPositive(row.Credit));
            UploadExactAmount gross = Sum(customers, row => row.Credit - row.Debit);
            UploadExactAmount inflows = Sum(treasury, row => Positive(row.Debit) + NegativeAsPositive(row.Credit));
            UploadExactAmount outflows = Sum(treasury, row => Positive(row.Credit) + NegativeAsPositive(row.Debit));
            UploadExactAmount bankOutflows = Sum(treasury.Where(row => accountTypes[row.AccountCode] == "Bank"),
                row => Positive(row.Credit) + NegativeAsPositive(row.Debit));
            UploadExactAmount charges = Sum(deductions.Where(row => chargeCategories.Contains(row.Category)), row => row.Debit - row.Credit);
            UploadExactAmount withholding = Sum(deductions.Where(row => withholdingCategories.Contains(row.Category)), row => row.Debit - row.Credit);
            UploadExactAmount other = Sum(deductions.Where(row => !chargeCategories.Contains(row.Category)
                && !withholdingCategories.Contains(row.Category)), row => row.Debit - row.Credit);
            UploadExactAmount deductionTotal = charges + withholding + other;
            UploadExactAmount netSettlement = inflows - outflows;
            UploadExactAmount expected = gross - deductionTotal;
            UploadExactAmount variance = netSettlement - expected;
            var customerCodes = customers.Select(row => row.AccountCode).Distinct(StringComparer.Ordinal).ToArray();
            var treasuryCodes = treasury.Select(row => row.AccountCode).Distinct(StringComparer.Ordinal).ToArray();
            var types = treasuryCodes.Select(code => accountTypes[code]).Distinct(StringComparer.Ordinal).ToArray();
            var onePeriod = all.Select(row => row.PeriodKey).Distinct(StringComparer.Ordinal).Count() == 1;
            bool reversed = gross.CompareTo(UploadExactAmount.Zero - Tolerance) < 0;
            bool grossPattern = treasuryCodes.Length == 1 && types.Length == 1 && types[0] == "Bank"
                && (reversed ? inflows.CompareTo(Tolerance) > 0 : bankOutflows.CompareTo(Tolerance) > 0);
            string method = grossPattern ? "Gross" : treasuryCodes.Length == 1 ? "Net" : "Review";
            bool unrelated = all.Any(row => !accountTypes.ContainsKey(row.AccountCode)
                && !customerCategories.Contains(row.Category)
                && !chargeCategories.Contains(row.Category) && !withholdingCategories.Contains(row.Category)
                && !otherCategories.Contains(row.Category)
                && (row.Debit.Abs().CompareTo(Tolerance) > 0 || row.Credit.Abs().CompareTo(Tolerance) > 0));
            bool matched = method is "Gross" or "Net" && customerCodes.Length == 1
                && treasuryCodes.Length == 1 && types.Length == 1 && onePeriod && !unrelated
                && variance.Abs().CompareTo(Tolerance) <= 0;
            string status = matched ? "Matched" : "Review required";
            UploadExactAmount technicalOffset = reversed && treasuryCodes.Length == 1 && types[0] == "Bank"
                ? UploadExactAmount.Zero - inflows : bankOutflows;
            UploadExactAmount cashflowAmount = string.Equals(presentation, "net", StringComparison.OrdinalIgnoreCase) && matched
                ? netSettlement : gross;
            string Join(IEnumerable<string> values) => string.Join(" / ", values.Where(value => !string.IsNullOrWhiteSpace(value))
                .Distinct(StringComparer.Ordinal));
            string? netValue = matched ? netSettlement.ToFixedString() : null;
            string? varianceValue = matched ? variance.ToFixedString() : null;
            results.Add(new(voucher.Key, all.Min(row => row.Date).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                customers[0].PeriodKey, customers[0].PeriodLabel, Join(customers.Select(row => row.AccountCode)),
                Join(customers.Select(row => row.AccountName)), Join(treasury.Select(row => row.AccountCode)),
                Join(treasury.Select(row => row.AccountName)), Join(types), method, presentation,
                gross.ToFixedString(), receipts.ToFixedString(), reversals.ToFixedString(), cashflowAmount.ToFixedString(),
                inflows.ToFixedString(), charges.ToFixedString(), withholding.ToFixedString(), other.ToFixedString(),
                deductionTotal.ToFixedString(), technicalOffset.ToFixedString(), netValue, expected.ToFixedString(),
                varianceValue, status, all.Select(row => row.Detail).FirstOrDefault(value => value.Length > 0) ?? ""));
        }
        return results.OrderBy(row => row.Date, StringComparer.Ordinal).ThenBy(row => row.VoucherId, StringComparer.Ordinal).ToArray();
    }

    private static IReadOnlyList<CashflowTransferDetail> BuildTransferDetails(
        IReadOnlyList<Line> rows, IReadOnlyDictionary<string, string> accountTypes, bool matchInternalTransfers,
        CancellationToken ct)
    {
        var results = new List<CashflowTransferDetail>();
        foreach (var voucher in rows.GroupBy(row => (row.Voucher, row.PeriodKey)))
        {
            ct.ThrowIfCancellationRequested();
            var treasury = voucher.Where(row => accountTypes.ContainsKey(row.AccountCode)).ToArray();
            var legs = treasury.GroupBy(row => row.AccountCode, StringComparer.Ordinal).Select(group => new
            {
                Code = group.Key, Name = group.Select(row => row.AccountName).FirstOrDefault(value => value.Length > 0) ?? "",
                Type = accountTypes[group.Key], Movement = group.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit)
            }).ToArray();
            if (legs.Select(leg => leg.Code).Distinct(StringComparer.Ordinal).Count() <= 1) continue;
            var transferors = legs.Where(leg => leg.Movement.CompareTo(UploadExactAmount.Zero - Tolerance) < 0).ToArray();
            var transferees = legs.Where(leg => leg.Movement.CompareTo(Tolerance) > 0).ToArray();
            UploadExactAmount transferorAmount = transferors.Aggregate(UploadExactAmount.Zero, (sum, leg) => sum + leg.Movement.Abs());
            UploadExactAmount transfereeAmount = transferees.Aggregate(UploadExactAmount.Zero, (sum, leg) => sum + leg.Movement);
            UploadExactAmount variance = transfereeAmount - transferorAmount;
            int nonTreasury = voucher.Count(row => !accountTypes.ContainsKey(row.AccountCode));
            bool balanced = variance.Abs().CompareTo(Tolerance) <= 0;
            bool simple = transferors.Length == 1 && transferees.Length == 1;
            string classification = nonTreasury > 0 ? "mixed" : simple && balanced ? "confirmed" : "ambiguous";
            string status = classification switch
            {
                "confirmed" => "Confirmed", "mixed" => "Review required - mixed voucher",
                _ => "Review required - multi-leg or unbalanced"
            };
            string transferorType = Join(transferors.Select(row => row.Type));
            string transfereeType = Join(transferees.Select(row => row.Type));
            bool matched = matchInternalTransfers && classification == "confirmed" && transferorType.Length > 0
                && transferorType == transfereeType;
            DateOnly date = voucher.Min(row => row.Date);
            var period = voucher.First();
            UploadExactAmount bankMovement = treasury.Where(row => accountTypes[row.AccountCode] == "Bank")
                .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
            UploadExactAmount cashMovement = treasury.Where(row => accountTypes[row.AccountCode] == "Cash")
                .Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Debit - row.Credit);
            results.Add(new(voucher.Key.Voucher, date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                voucher.Key.PeriodKey, period.PeriodLabel,
                (transferorType.Length == 0 ? "Unknown" : transferorType) + " to " + (transfereeType.Length == 0 ? "Unknown" : transfereeType),
                Join(transferors.Select(row => row.Code)), Join(transferors.Select(row => row.Name)), transferorType,
                Join(transferees.Select(row => row.Code)), Join(transferees.Select(row => row.Name)), transfereeType,
                transferorAmount.ToFixedString(), transfereeAmount.ToFixedString(), transfereeAmount.ToFixedString(),
                variance.ToFixedString(), classification, status, matched, transferors.Length, transferees.Length,
                nonTreasury, voucher.Select(row => row.Detail).FirstOrDefault(value => value.Length > 0) ?? "",
                bankMovement.ToFixedString(), cashMovement.ToFixedString()));
        }
        return results.OrderBy(row => row.Date, StringComparer.Ordinal).ThenBy(row => row.VoucherId, StringComparer.Ordinal).ToArray();

        static string Join(IEnumerable<string> values) => string.Join(" / ", values.Where(value => !string.IsNullOrWhiteSpace(value)).Distinct(StringComparer.Ordinal));
    }

    private static IReadOnlyList<CashflowTreasuryCategoryTotal> BuildTreasuryCategoryTotals(
        IReadOnlyList<Line> rows, IReadOnlyDictionary<string, string> accountTypes, string presentation,
        IReadOnlyList<CashflowCustomerPaymentDetail> customerDetails,
        IReadOnlyList<CashflowPeriod> periods, IReadOnlyDictionary<string, HashSet<string>> roles, CancellationToken ct)
    {
        var amounts = new Dictionary<(string Category, string Period, string Type), UploadExactAmount>();
        var customerCategories = roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase);
        var customerByVoucher = customerDetails.ToDictionary(row => row.VoucherId, StringComparer.Ordinal);
        foreach (var voucher in rows.GroupBy(row => row.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var treasuryTypes = voucher.Where(row => accountTypes.ContainsKey(row.AccountCode))
                .Select(row => accountTypes[row.AccountCode]).Distinct(StringComparer.Ordinal).ToArray();
            var contra = voucher.Where(row => !accountTypes.ContainsKey(row.AccountCode)).ToArray();
            string bucket = treasuryTypes.Length == 1 ? treasuryTypes[0] : "mixed";
            customerByVoucher.TryGetValue(voucher.Key, out var customer);
            var firstCustomer = contra.FirstOrDefault(line => customerCategories.Contains(line.Category));
            foreach (var line in contra)
            {
                UploadExactAmount amount = line.Credit - line.Debit;
                if (string.Equals(presentation, "net", StringComparison.OrdinalIgnoreCase)
                    && customer?.Status == "Matched" && customerCategories.Contains(line.Category))
                {
                    if (!ReferenceEquals(firstCustomer, line)) continue;
                    amount = UploadExactAmount.Parse(customer.CashflowAmount);
                }
                var key = (line.Category, line.PeriodKey, bucket);
                amounts[key] = amounts.GetValueOrDefault(key) + amount;
            }
        }
        return amounts.Keys.Select(key => new CashflowTreasuryCategoryTotal(key.Category, key.Period,
                amounts.GetValueOrDefault((key.Category, key.Period, "Bank")).ToFixedString(),
                amounts.GetValueOrDefault((key.Category, key.Period, "Cash")).ToFixedString(),
                amounts.GetValueOrDefault((key.Category, key.Period, "mixed")).ToFixedString(),
                (amounts.GetValueOrDefault((key.Category, key.Period, "Bank"))
                    + amounts.GetValueOrDefault((key.Category, key.Period, "Cash"))
                    + amounts.GetValueOrDefault((key.Category, key.Period, "mixed"))).ToFixedString()))
            .OrderBy(row => row.Category, StringComparer.OrdinalIgnoreCase)
            .ThenBy(row => periods.FirstOrDefault(period => period.Key == row.PeriodKey)?.Start ?? "", StringComparer.Ordinal)
            .ToArray();
    }

    private static IReadOnlyList<CashflowVendorPaymentDetail> BuildVendorDetails(
        IReadOnlyList<Line> rows, IReadOnlySet<string> vendorCodes, CancellationToken ct)
    {
        var result = new List<CashflowVendorPaymentDetail>();
        foreach (var line in rows.Where(row => vendorCodes.Contains(row.AccountCode))
            .OrderBy(row => row.Date).ThenBy(row => row.Voucher, StringComparer.Ordinal).ThenBy(row => row.AccountCode, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            UploadExactAmount payments = Positive(line.Debit) + NegativeAsPositive(line.Credit);
            UploadExactAmount reversals = Positive(line.Credit) + NegativeAsPositive(line.Debit);
            UploadExactAmount net = line.Debit - line.Credit;
            result.Add(new(line.Voucher, line.Date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), line.PeriodKey,
                line.PeriodLabel, line.AccountCode, line.AccountName, line.Category, payments.ToFixedString(),
                reversals.ToFixedString(), net.ToFixedString(), (UploadExactAmount.Zero - net).ToFixedString(), line.Detail));
        }
        return result;
    }

    private static IReadOnlyList<CashflowVendorPaymentTotal> BuildVendorTotals(
        IReadOnlyList<CashflowVendorPaymentDetail> details, bool byPeriod)
    {
        CashflowVendorPaymentTotal Build(IEnumerable<CashflowVendorPaymentDetail> source,
            string account, string category, string? key, string? label)
        {
            var rows = source.ToArray();
            return new(account, JoinUnique(rows.Select(row => row.AccountName)), category,
                rows.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(row.Payments)).ToFixedString(),
                rows.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(row.Reversals)).ToFixedString(),
                rows.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(row.NetPayments)).ToFixedString(),
                rows.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + UploadExactAmount.Parse(row.CashflowAmount)).ToFixedString(),
                rows.Select(row => row.VoucherId).Distinct(StringComparer.Ordinal).Count(), key, label);
        }
        var result = new List<CashflowVendorPaymentTotal>();
        if (byPeriod)
        {
            foreach (var group in details.GroupBy(row => (row.PeriodKey, row.PeriodLabel, row.AccountCode, row.Category)))
                result.Add(Build(group, group.Key.AccountCode, group.Key.Category, group.Key.PeriodKey, group.Key.PeriodLabel));
        }
        else
        {
            foreach (var group in details.GroupBy(row => (row.AccountCode, row.Category)))
                result.Add(Build(group, group.Key.AccountCode, group.Key.Category, null, null));
        }
        return result
            .OrderBy(row => row.PeriodKey, StringComparer.Ordinal)
            .ThenBy(row => row.AccountName, StringComparer.OrdinalIgnoreCase)
            .ThenBy(row => row.AccountCode, StringComparer.Ordinal)
            .ThenBy(row => row.Category, StringComparer.OrdinalIgnoreCase).ToArray();
    }

    private static IReadOnlyList<CashflowCustomerPaymentDetail> BuildCustomerAccountDetails(
        IReadOnlyList<Line> rows, IReadOnlyList<CashflowCustomerPaymentDetail> voucherDetails,
        IReadOnlyDictionary<string, string> accountTypes, IReadOnlyDictionary<string, HashSet<string>> roles,
        CancellationToken ct)
    {
        var customerCategories = roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase);
        var summaryByVoucher = voucherDetails.ToDictionary(row => row.VoucherId, StringComparer.Ordinal);
        var results = new List<CashflowCustomerPaymentDetail>();
        foreach (var voucher in rows.GroupBy(row => row.Voucher, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            if (!summaryByVoucher.TryGetValue(voucher.Key, out var summary)) continue;
            var customerLines = voucher.Where(row => !accountTypes.ContainsKey(row.AccountCode)
                && customerCategories.Contains(row.Category)).ToArray();
            int customerAccountCount = customerLines.Select(row => row.AccountCode).Distinct(StringComparer.Ordinal).Count();
            int customerPeriodCount = customerLines.Select(row => row.PeriodKey).Distinct(StringComparer.Ordinal).Count();
            foreach (var group in customerLines.GroupBy(row => (row.AccountCode, row.PeriodKey)))
            {
                var lines = group.ToArray();
                UploadExactAmount gross = lines.Aggregate(UploadExactAmount.Zero, (sum, row) => sum + row.Credit - row.Debit);
                UploadExactAmount receipts = lines.Aggregate(UploadExactAmount.Zero,
                    (sum, row) => sum + Positive(row.Credit) + NegativeAsPositive(row.Debit));
                UploadExactAmount reversals = lines.Aggregate(UploadExactAmount.Zero,
                    (sum, row) => sum + Positive(row.Debit) + NegativeAsPositive(row.Credit));
                var item = summary with
                {
                    CustomerAccountCode = group.Key.AccountCode,
                    CustomerAccountName = JoinUnique(lines.Select(row => row.AccountName)),
                    Date = lines.Min(row => row.Date).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                    PeriodKey = group.Key.PeriodKey,
                    PeriodLabel = lines[0].PeriodLabel,
                    CustomerGrossAmount = gross.ToFixedString(),
                    CustomerReceipts = receipts.ToFixedString(),
                    CustomerReversals = reversals.ToFixedString()
                };
                if (summary.Status != "Matched")
                {
                    item = item with { Status = "Review required", CashflowAmount = gross.ToFixedString() };
                    if (customerAccountCount != 1 || customerPeriodCount != 1)
                        item = item with
                        {
                            BankCharges = "0", WithholdingTax = "0", OtherDeductions = "0", DeductionsTotal = "0",
                            TechnicalBankOffset = "0", ExpectedNetSettlement = gross.ToFixedString(),
                            NetBankSettlement = null, Variance = null
                        };
                }
                results.Add(item);
            }
        }
        return results.OrderBy(row => row.Date, StringComparer.Ordinal)
            .ThenBy(row => row.VoucherId, StringComparer.Ordinal)
            .ThenBy(row => row.CustomerAccountCode, StringComparer.Ordinal).ToArray();
    }

    private static IReadOnlyList<CashflowPaymentSummary> BuildCustomerPaymentSummaries(
        IReadOnlyList<CashflowCustomerPaymentDetail> details) => details
        .GroupBy(row => row.CustomerAccountCode, StringComparer.Ordinal)
        .Select(group => Summarize(group, group.Key, null, null))
        .OrderBy(row => row.CustomerAccountName, StringComparer.OrdinalIgnoreCase)
        .ThenBy(row => row.CustomerAccountCode, StringComparer.Ordinal).ToArray();

    private static IReadOnlyList<CashflowPaymentSummary> CustomerTotalsByPeriod(
        IReadOnlyList<CashflowCustomerPaymentDetail> details) => details
        .GroupBy(row => (row.PeriodKey, row.PeriodLabel))
        .SelectMany(period => period.GroupBy(row => row.CustomerAccountCode, StringComparer.Ordinal)
            .Select(group => Summarize(group, group.Key, period.Key.PeriodKey, period.Key.PeriodLabel)))
        .OrderBy(row => row.PeriodKey, StringComparer.Ordinal)
        .ThenBy(row => row.CustomerAccountName, StringComparer.OrdinalIgnoreCase)
        .ThenBy(row => row.CustomerAccountCode, StringComparer.Ordinal).ToArray();

    private static CashflowPaymentSummary Summarize(IEnumerable<CashflowCustomerPaymentDetail> source,
        string accountCode, string? periodKey, string? periodLabel)
    {
        var rows = source.ToArray();
        UploadExactAmount Sum(Func<CashflowCustomerPaymentDetail, string?> amount) => rows.Aggregate(
            UploadExactAmount.Zero, (sum, row) => sum + ParseOptional(amount(row)));
        int voucherCount = rows.Select(row => row.VoucherId).Distinct(StringComparer.Ordinal).Count();
        int matchedCount = rows.Where(row => row.Status == "Matched").Select(row => row.VoucherId)
            .Distinct(StringComparer.Ordinal).Count();
        return new(accountCode, JoinUnique(rows.Select(row => row.CustomerAccountName)),
            JoinUnique(rows.Select(row => row.BankAccountType)), voucherCount, matchedCount,
            voucherCount - matchedCount, Sum(row => row.CustomerGrossAmount).ToFixedString(),
            Sum(row => row.CustomerReceipts).ToFixedString(), Sum(row => row.CustomerReversals).ToFixedString(),
            Sum(row => row.CashflowAmount).ToFixedString(), Sum(row => row.BankCharges).ToFixedString(),
            Sum(row => row.WithholdingTax).ToFixedString(), Sum(row => row.OtherDeductions).ToFixedString(),
            Sum(row => row.DeductionsTotal).ToFixedString(), Sum(row => row.TechnicalBankOffset).ToFixedString(),
            Sum(row => row.ExpectedNetSettlement).ToFixedString(), Sum(row => row.NetBankSettlement).ToFixedString(),
            Sum(row => row.CashflowAmount).ToFixedString(), Sum(row => row.Variance).ToFixedString(),
            matchedCount == voucherCount ? "Matched" : "Review required", periodKey, periodLabel);
    }

    private static IReadOnlyList<CashflowPaymentPeriodTotal> BuildCustomerPaymentPeriodTotals(
        IReadOnlyList<CashflowCustomerPaymentDetail> details,
        IReadOnlyList<CashflowCustomerPaymentDetail> voucherDetails,
        IReadOnlyDictionary<string, Dictionary<string, AmountByPeriod>> categoryTotals,
        IReadOnlyDictionary<string, HashSet<string>> roles, IReadOnlyList<CashflowPeriod> periods)
    {
        var receipts = roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase);
        return periods.Select(period =>
        {
            var selected = details.Where(row => row.PeriodKey == period.Key).ToArray();
            UploadExactAmount Sum(Func<CashflowCustomerPaymentDetail, string?> amount) => selected.Aggregate(
                UploadExactAmount.Zero, (sum, row) => sum + ParseOptional(amount(row)));
            var vouchers = selected.Select(row => row.VoucherId).Distinct(StringComparer.Ordinal).ToArray();
            int matched = selected.Where(row => row.Status == "Matched").Select(row => row.VoucherId)
                .Distinct(StringComparer.Ordinal).Count();
            UploadExactAmount mainTotal = UploadExactAmount.Zero;
            foreach (string category in receipts)
                mainTotal += categoryTotals.GetValueOrDefault(category)?.GetValueOrDefault(period.Key)?.Value ?? UploadExactAmount.Zero;
            UploadExactAmount eligible = Sum(row => row.CashflowAmount);
            UploadExactAmount difference = eligible - mainTotal;
            return new CashflowPaymentPeriodTotal(period.Key, period.Label, period.Start, period.End,
                vouchers.Length, matched, vouchers.Length - matched,
                Sum(row => row.CustomerGrossAmount).ToFixedString(), Sum(row => row.CustomerReceipts).ToFixedString(),
                Sum(row => row.CustomerReversals).ToFixedString(), eligible.ToFixedString(),
                Sum(row => row.BankCharges).ToFixedString(), Sum(row => row.WithholdingTax).ToFixedString(),
                Sum(row => row.OtherDeductions).ToFixedString(), Sum(row => row.DeductionsTotal).ToFixedString(),
                Sum(row => row.TechnicalBankOffset).ToFixedString(), Sum(row => row.ExpectedNetSettlement).ToFixedString(),
                Sum(row => row.NetBankSettlement).ToFixedString(), eligible.ToFixedString(), mainTotal.ToFixedString(),
                difference.ToFixedString(), matched == vouchers.Length && difference.Abs().CompareTo(Tolerance) <= 0
                    ? "Matched" : "Review required");
        }).ToArray();
    }

    private static CashflowCustomerPaymentReconciliation BuildCustomerPaymentReconciliation(
        IReadOnlyList<CashflowCustomerPaymentDetail> details, IReadOnlyList<CashflowPaymentSummary> customerTotals,
        IReadOnlyDictionary<string, Dictionary<string, AmountByPeriod>> categoryTotals,
        IReadOnlyDictionary<string, HashSet<string>> roles)
    {
        UploadExactAmount eligible = customerTotals.Aggregate(UploadExactAmount.Zero,
            (sum, row) => sum + UploadExactAmount.Parse(row.StatementEligibleActualSettlement));
        UploadExactAmount main = UploadExactAmount.Zero;
        foreach (string category in roles.GetValueOrDefault("customer_receipt") ?? new(StringComparer.OrdinalIgnoreCase))
            if (categoryTotals.TryGetValue(category, out var amounts))
                main += amounts.Values.Aggregate(UploadExactAmount.Zero, (sum, amount) => sum + amount.Value);
        UploadExactAmount difference = eligible - main;
        int matched = details.Count(row => row.Status == "Matched");
        return new(eligible.ToFixedString(), main.ToFixedString(), difference.ToFixedString(),
            difference.Abs().CompareTo(Tolerance) <= 0 ? "Matched" : "Review required", customerTotals.Count,
            matched, details.Count - matched, Tolerance.ToFixedString());
    }

    private static CashflowTransferReconciliation BuildTransferReconciliation(
        IReadOnlyList<CashflowTransferDetail> details)
    {
        int confirmed = details.Count(row => row.Classification == "confirmed");
        int mixed = details.Count(row => row.Classification == "mixed");
        int ambiguous = details.Count(row => row.Classification == "ambiguous");
        UploadExactAmount transferors = details.Aggregate(UploadExactAmount.Zero,
            (sum, row) => sum + UploadExactAmount.Parse(row.TransferorAmount));
        UploadExactAmount transferees = details.Aggregate(UploadExactAmount.Zero,
            (sum, row) => sum + UploadExactAmount.Parse(row.TransfereeAmount));
        UploadExactAmount variance = details.Aggregate(UploadExactAmount.Zero,
            (sum, row) => sum + UploadExactAmount.Parse(row.Variance));
        string status = details.Count == 0 ? "No transfers"
            : confirmed == details.Count && variance.Abs().CompareTo(Tolerance) <= 0 ? "Matched" : "Review required";
        return new(details.Count, confirmed, mixed, ambiguous, transferors.ToFixedString(),
            transferees.ToFixedString(), variance.ToFixedString(), status);
    }

    private static CashflowTreasuryReconciliation BuildTreasuryReconciliation(
        IReadOnlyDictionary<string, Dictionary<string, AmountByPeriod>> categoryTotals,
        IReadOnlyList<CashflowTreasuryCategoryTotal> treasuryTotals)
    {
        UploadExactAmount maximum = UploadExactAmount.Zero, mixedAmount = UploadExactAmount.Zero;
        int mixedCount = 0;
        foreach (var row in treasuryTotals)
        {
            var splitTotal = UploadExactAmount.Parse(row.BankAmount) + UploadExactAmount.Parse(row.CashAmount)
                + UploadExactAmount.Parse(row.MixedAmount);
            var expected = categoryTotals.GetValueOrDefault(row.Category)?.GetValueOrDefault(row.PeriodKey)?.Value
                ?? UploadExactAmount.Zero;
            var variance = (splitTotal - expected).Abs();
            if (variance.CompareTo(maximum) > 0) maximum = variance;
            var mixed = UploadExactAmount.Parse(row.MixedAmount);
            mixedAmount += mixed;
            if (mixed.Abs().CompareTo(Tolerance) > 0) mixedCount++;
        }
        string sourceStatus = maximum.CompareTo(Tolerance) <= 0 ? "Matched" : "Variance";
        string allocationStatus = mixedCount > 0 ? "Review required" : "Proven";
        string status = sourceStatus != "Matched" ? "Variance" : mixedCount > 0 ? "Review required" : "Matched";
        return new(status, maximum.ToFixedString(), mixedAmount.ToFixedString(), mixedCount,
            "single Treasury type only; multi-type categories remain unallocated; source legs prove movements separately",
            sourceStatus, maximum.ToFixedString(), allocationStatus);
    }

    private static IReadOnlyDictionary<string, CashflowPeriodMetric> BuildPeriodMetrics(
        IReadOnlyList<Line> rows, IReadOnlyList<CashflowTransferDetail> transfers,
        IReadOnlyList<CashflowReviewLine> reviews, IReadOnlyDictionary<string, HashSet<string>> roles,
        IReadOnlyList<CashflowPeriod> periods)
    {
        var deductionCategories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string role in new[] { "bank_charge", "withholding_tax", "other_deduction" })
            if (roles.TryGetValue(role, out var categories)) deductionCategories.UnionWith(categories);
        return periods.ToDictionary(period => period.Key, period =>
        {
            var scoped = rows.Where(row => row.PeriodKey == period.Key).ToArray();
            return new CashflowPeriodMetric(period.Key, scoped.Length,
                scoped.Select(row => row.Voucher).Distinct(StringComparer.Ordinal).Count(),
                transfers.Count(row => row.PeriodKey == period.Key),
                reviews.Count(row => row.PeriodKey == period.Key),
                scoped.Count(row => deductionCategories.Contains(row.Category)));
        }, StringComparer.Ordinal);
    }

    private static IReadOnlyList<CashflowDeductionDetail> BuildDeductionDetails(
        IReadOnlyList<Line> rows, IReadOnlyList<CashflowCustomerPaymentDetail> customerDetails,
        IReadOnlyDictionary<string, string> accountTypes, IReadOnlyDictionary<string, HashSet<string>> roles,
        string presentation)
    {
        var deductionCategories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string role in new[] { "bank_charge", "withholding_tax", "other_deduction" })
            if (roles.TryGetValue(role, out var categories)) deductionCategories.UnionWith(categories);
        var customerByVoucher = customerDetails.ToDictionary(row => row.VoucherId, StringComparer.Ordinal);
        return rows.Where(row => !accountTypes.ContainsKey(row.AccountCode) && deductionCategories.Contains(row.Category))
            .OrderBy(row => row.Date).ThenBy(row => row.Id)
            .Select(row => new CashflowDeductionDetail(row.Category, row.Date.ToString("yyyy-MM", CultureInfo.InvariantCulture), row.PeriodKey, row.PeriodLabel,
                (row.Credit - row.Debit).ToFixedString(), row.AccountCode, row.AccountName,
                row.Date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                !string.Equals(presentation, "net", StringComparison.OrdinalIgnoreCase) ? "shown_separately"
                    : customerByVoucher.TryGetValue(row.Voucher, out var customer)
                        ? customer.Status == "Matched" ? "netted_from_customer_receipt" : "review_required"
                        : "shown_separately"))
            .ToArray();
    }

    private static UploadExactAmount ParseOptional(string? amount) => string.IsNullOrWhiteSpace(amount)
        ? UploadExactAmount.Zero : UploadExactAmount.Parse(amount);

    private static string JoinUnique(IEnumerable<string> values) => string.Join(" / ", values
        .Where(value => !string.IsNullOrWhiteSpace(value)).Distinct(StringComparer.Ordinal));

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
