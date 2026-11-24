using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.FinancialStatements;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.Reports;

/// <summary>Compares Cashflows to independent imported ledger evidence without guessing missing balances.</summary>
internal sealed class FinancialReportReconciliationService
{
    private sealed record LedgerLine(int Id, string Voucher, DateOnly Date, UploadExactAmount Debit,
        UploadExactAmount Credit, int Version, string? Fingerprint);
    private sealed record Anchor(ReportCashflowLedgerEvidenceUpload Upload, DateOnly Start, DateOnly End,
        UploadExactAmount Opening, UploadExactAmount Closing, string OpeningSource,
        IReadOnlyList<LedgerLine> Lines);
    private static readonly UploadExactAmount Tolerance = UploadExactAmount.Parse("0.01");

    public FinancialReportReconciliationResult Calculate(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows, CancellationToken ct = default)
    {
        if (snapshot.Scope.Kind != ReportInputKind.LedgerSource
            || snapshot.Scope.CategorySource != ReportAccountCategorySource.Cashflow
            || !snapshot.CashflowLedgerEvidenceUploads.Requested || !snapshot.CashflowLedgerEvidenceUploads.Complete
            || !snapshot.CashflowLedgerEvidenceRows.Requested || !snapshot.CashflowLedgerEvidenceRows.Complete)
            throw new FinancialStatementInputException("A complete independent ledger evidence snapshot is required for reconciliation.");
        if (!StringComparer.Ordinal.Equals(snapshot.Identity.Fingerprint, cashflows.InputFingerprint))
            throw new FinancialStatementInputException("Cashflows and ledger evidence do not share the same captured source identity.");

        var accountTypes = snapshot.CashflowTreasuryAccounts.Items.GroupBy(row => row.AccountCode.Trim(), StringComparer.Ordinal)
            .ToDictionary(group => group.Key,
                group => group.Last().TreasuryType.Equals("cash", StringComparison.OrdinalIgnoreCase) ? "Cash" : "Bank",
                StringComparer.Ordinal);
        if (snapshot.Scope.SelectedAccountCodes is { } selectedAccounts)
        {
            if (selectedAccounts.Count == 0 || selectedAccounts.Any(code => !accountTypes.ContainsKey(code)))
                throw new FinancialStatementInputException("Selected Treasury accounts are unavailable for reconciliation.");
            accountTypes = accountTypes.Where(pair => selectedAccounts.Contains(pair.Key))
                .ToDictionary(pair => pair.Key,pair => pair.Value,StringComparer.Ordinal);
        }
        var accountResults = new List<FinancialReportAccountReconciliation>();
        var diagnostics = new List<string>();
        foreach (var (code, type) in accountTypes.OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var metadata = snapshot.CashflowLedgerEvidenceUploads.Items.Where(row => row.AccountCode == code)
                .OrderBy(row => row.Id).ToArray();
            var linesByUpload = snapshot.CashflowLedgerEvidenceRows.Items.Where(row => metadata.Any(upload => upload.Id == row.UploadId))
                .GroupBy(row => row.UploadId).ToDictionary(group => group.Key, group => group.ToArray());
            var anchors = new List<Anchor>();
            var failed = false;
            if (metadata.Length == 0) diagnostics.Add($"No imported independent ledger is available for Cash/Bank account {code}.");
            if (metadata.Select(row => row.TemplateId).Distinct().Count() > 1)
            { diagnostics.Add($"Several ledger templates are configured for account {code}; balance evidence is unavailable."); failed = true; }

            foreach (var upload in metadata)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    using var evidenceDoc = JsonDocument.Parse(upload.EvidenceJson ?? "{}");
                    var evidence = evidenceDoc.RootElement;
                    var check = evidence.TryGetProperty("balance_check", out var checkElement) ? checkElement : default;
                    if (check.ValueKind != JsonValueKind.Object || !ConfigurationCurrent(upload, check))
                    { diagnostics.Add($"Ledger setup for account {code} changed; revalidate the ledger upload."); continue; }
                    if (!TryDate(upload.PeriodStart, out var start) || !TryDate(upload.PeriodEnd, out var end) || start > end)
                        throw new FormatException();
                    var lines = ParseLines(upload, linesByUpload.GetValueOrDefault(upload.Id) ?? []);
                    string fingerprint = CanonicalFingerprint(lines.Where(row => row.Date >= start && row.Date <= end));
                    if (!TryString(check, "canonical_fingerprint", out var savedFingerprint)
                        || !StringComparer.OrdinalIgnoreCase.Equals(fingerprint, savedFingerprint))
                    { diagnostics.Add($"Ledger proof for account {code} changed; current balances require revalidation."); continue; }
                    if (!TryString(check, "source_mode", out var sourceMode) || sourceMode != "replace_range"
                        || !TryString(check, "status", out var proofStatus) || proofStatus != "consistent"
                        || !TryString(check, "opening_source", out var openingSource)
                        || openingSource is not ("erp_opening" or "inferred_from_transaction" or "inferred_from_closing"))
                        continue;
                    if (!TryAmount(check, "opening", out var opening) || !TryAmount(check, "closing", out var closing))
                        throw new FormatException();
                    var rangeMovement = Sum(lines.Where(row => row.Date >= start && row.Date <= end));
                    if ((opening + rangeMovement - closing).Abs().CompareTo(Tolerance) > 0)
                        throw new FormatException();
                    anchors.Add(new(upload,start,end,opening,closing,openingSource,lines));
                }
                catch (Exception error) when (error is JsonException or FormatException or OverflowException or InvalidOperationException)
                { diagnostics.Add($"Ledger evidence for account {code} is invalid; its balances are unavailable."); failed = true; }
            }

            var accountPeriods = new List<FinancialReportPeriodReconciliation>();
            foreach (var period in cashflows.Periods)
            {
                if (!TryDate(period.Start, out var start) || !TryDate(period.End, out var end) || end == DateOnly.MaxValue)
                    throw new FinancialStatementInputException("The selected reconciliation period is invalid.");
                var periodAnchors = anchors.Where(anchor => anchor.Start <= start && anchor.End >= end).ToArray();
                if (failed || periodAnchors.Length == 0)
                {
                    accountPeriods.Add(new(period.Key,"Unavailable",null,null,null,null,null,null));
                    continue;
                }
                var balances = periodAnchors.Select(anchor => (
                    Opening: anchor.Opening + Sum(anchor.Lines.Where(row => row.Date >= anchor.Start && row.Date < start)),
                    Closing: anchor.Opening + Sum(anchor.Lines.Where(row => row.Date >= anchor.Start && row.Date <= end)),
                    Movement: Sum(anchor.Lines.Where(row => row.Date >= start && row.Date <= end)))).ToArray();
                if (balances.Select(row => row.Opening.ToFixedString()).Distinct(StringComparer.Ordinal).Count() > 1
                    || balances.Select(row => row.Closing.ToFixedString()).Distinct(StringComparer.Ordinal).Count() > 1)
                {
                    accountPeriods.Add(new(period.Key,"Review required",null,null,null,null,null,null));
                    diagnostics.Add($"Current independent ledger anchors disagree for {code} in {period.Key}.");
                    continue;
                }
                var balance = balances[0];
                var cashflowMovement = cashflows.TreasuryPostings
                    .Where(row => row.AccountCode == code
                        && DateOnly.TryParseExact(row.Date, "yyyy-MM-dd", CultureInfo.InvariantCulture,
                            DateTimeStyles.None, out var postingDate)
                        && postingDate >= start && postingDate <= end)
                    .Aggregate(UploadExactAmount.Zero,
                        (sum, row) => sum + UploadExactAmount.Parse(row.Debit) - UploadExactAmount.Parse(row.Credit));
                var calculated = balance.Opening + cashflowMovement;
                var difference = balance.Closing - calculated;
                accountPeriods.Add(new(period.Key,difference.Abs().CompareTo(Tolerance)<=0?"Matched":"Differences",
                    balance.Opening.ToFixedString(),balance.Closing.ToFixedString(),balance.Movement.ToFixedString(),
                    cashflowMovement.ToFixedString(),calculated.ToFixedString(),difference.ToFixedString()));
            }
            string name = metadata.LastOrDefault()?.AccountName is { Length: > 0 } value && value != code ? value : code;
            accountResults.Add(new(code,name,type,accountPeriods));
        }

        var postingFindings = ComparePostings(snapshot, cashflows, accountTypes, accountResults, diagnostics, ct);
        var (presentationAdjustments, voucherReviews) = BuildPresentationAdjustments(
            cashflows, accountTypes, snapshot.Scope.SelectedAccountCodes, ct);
        var adjustmentScopes=presentationAdjustments.Select(row=>(row.TreasuryView,row.VoucherId,row.PeriodKey))
            .ToHashSet();
        var adjustmentEvidence=cashflows.ReconciliationLines.SelectMany(row=>adjustmentScopes
                .Where(scope=>scope.VoucherId==row.VoucherId)
                .Select(scope=>new FinancialReportAdjustmentEvidence(scope.TreasuryView,row.SourceRowId,row.VoucherId,
                    row.Date,scope.PeriodKey,row.AccountCode,row.AccountName,row.Category,row.Debit,row.Credit,
                    row.IsTreasury,row.CategoryOwned,row.IsInPeriod))).ToArray();
        if (voucherReviews.Count > 0)
            diagnostics.Add($"{voucherReviews.Count} voucher-period allocation(s) require review before presentation agreement can be accepted.");
        string postingStatus = postingFindings.Count == 0||postingFindings.Any(row=>row.Status=="unavailable") ? "Unavailable"
            : postingFindings.Any(row => row.Status == "unresolved") ? "Review required"
            : postingFindings.Any(row => row.Status != "matched") ? "Differences" : "Matched";
        string status = accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Unavailable")
            ? "Unavailable" : accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Review required")
                ? "Review required" : accountResults.SelectMany(row => row.Periods).Any(row => row.Status == "Differences")
                    ? "Differences" : "Matched";
        string digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(snapshot.Identity.Fingerprint
            + "|" + string.Join("|", accountResults.SelectMany(row => row.Periods).Select(row => row.Difference ?? "unavailable"))
            + "|" + string.Join("|", presentationAdjustments.Select(row => $"{row.TreasuryView}:{row.VoucherId}:{row.PeriodKey}:{row.Reason}:{row.Receipts}:{row.Payments}"))))).ToLowerInvariant();
        var reconciliation=new FinancialReportReconciliationResult(status,digest,postingStatus,accountResults,postingFindings,diagnostics)
        {PresentationAdjustments=presentationAdjustments,VoucherReviews=voucherReviews,AdjustmentEvidence=adjustmentEvidence};
        return reconciliation with {WorkingPaper=BuildWorkingPaper(snapshot,cashflows,reconciliation,digest,ct)};
    }

    private static (IReadOnlyList<FinancialReportPresentationAdjustment> Adjustments,
        IReadOnlyList<FinancialReportVoucherReview> Reviews) BuildPresentationAdjustments(
        CashflowCalculationResult cashflows,IReadOnlyDictionary<string,string> accountTypes,
        IReadOnlyList<string>? selectedCodes,CancellationToken ct)
    {
        var selected=(selectedCodes??accountTypes.Keys.ToArray()).ToHashSet(StringComparer.Ordinal);
        if(selected.Count==0||selected.Any(code=>!accountTypes.ContainsKey(code)))
            throw new FinancialStatementInputException("Selected Treasury accounts are unavailable for reconciliation.");
        var source=cashflows.ReconciliationLines;
        var views=cashflows.TreasuryView=="split"?new[]{"combined","bank","cash"}:new[]{cashflows.TreasuryView};
        var adjustments=new List<FinancialReportPresentationAdjustment>();
        var reviews=new List<FinancialReportVoucherReview>();
        foreach(string view in views)
        {
            ct.ThrowIfCancellationRequested();
            var column=view=="bank"?"bank":view=="cash"?"cash":null;
            var categorySections=cashflows.CategoryTotals.ToDictionary(row=>row.Category,row=>
            {
                UploadExactAmount total;
                if(column is null) total=UploadExactAmount.Parse(row.Total);
                else total=cashflows.TreasuryCategoryTotals.Where(item=>item.Category==row.Category)
                    .Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+UploadExactAmount.Parse(column=="bank"?item.BankAmount:item.CashAmount));
                return total.CompareTo(UploadExactAmount.Zero)<0?"payments":"receipts";
            },StringComparer.OrdinalIgnoreCase);
            foreach(var voucher in source.Where(row=>row.IsInPeriod&&row.IsTreasury&&selected.Contains(row.AccountCode)
                    &&(column is null||accountTypes[row.AccountCode].Equals(column,StringComparison.OrdinalIgnoreCase)))
                .GroupBy(row=>(row.VoucherId,row.PeriodKey)))
            {
                ct.ThrowIfCancellationRequested();
                var treasury=voucher.ToArray();
                var related=source.Where(row=>row.VoucherId==voucher.Key.VoucherId&&row.PeriodKey==voucher.Key.PeriodKey).ToArray();
                var categoryRows=related.Where(row=>!row.IsTreasury).ToArray();
                var owned=categoryRows.Where(row=>row.CategoryOwned).ToArray();
                UploadExactAmount inflow=UploadExactAmount.Zero,outflow=UploadExactAmount.Zero;
                foreach(var row in treasury)
                {
                    var debit=UploadExactAmount.Parse(row.Debit);var credit=UploadExactAmount.Parse(row.Credit);
                    inflow+=Positive(debit)+NegativeAsPositive(credit);
                    outflow+=Positive(credit)+NegativeAsPositive(debit);
                }
                if(inflow==UploadExactAmount.Zero&&outflow==UploadExactAmount.Zero)continue;
                if(categoryRows.Length>0&&owned.Length==0)
                {
                    Add(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"selected_allocation_review",
                        "Selected Treasury movements shown separately; allocation requires review","",UploadExactAmount.Zero-inflow,UploadExactAmount.Zero-outflow);
            reviews.Add(new(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"Counter-account allocation is not proven for the selected Treasury accounts."));
                    continue;
                }
                if(categoryRows.Length==0)
                {
                    Add(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"internal_transfers",
                        "Internal transfers shown separately","",UploadExactAmount.Zero-inflow,UploadExactAmount.Zero-outflow);
                    continue;
                }
                var counterNet=owned.Aggregate(UploadExactAmount.Zero,(sum,row)=>
                    sum+UploadExactAmount.Parse(row.Credit)-UploadExactAmount.Parse(row.Debit));
                if((inflow-outflow-counterNet).Abs().CompareTo(Tolerance)>0)
                {
                    reviews.Add(new(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"Voucher Treasury and counter-account movements do not balance."));
                    continue;
                }
                var offset=inflow.CompareTo(outflow)<0?inflow:outflow;
                Add(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"treasury_offsets",
                    "Opposite Treasury postings within vouchers","",UploadExactAmount.Zero-offset,UploadExactAmount.Zero-offset);
                foreach(var row in owned)
                {
                    var amount=UploadExactAmount.Parse(row.Credit)-UploadExactAmount.Parse(row.Debit);
                    if((counterNet.CompareTo(UploadExactAmount.Zero)>=0&&amount.CompareTo(UploadExactAmount.Zero)<0)
                        ||(counterNet.CompareTo(UploadExactAmount.Zero)<0&&amount.CompareTo(UploadExactAmount.Zero)>0))
                        Add(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"counterpart_gross",
                            "Opposing counter-account amounts: gross presentation",row.Category,amount.Abs(),amount.Abs());
                    string section=categorySections.GetValueOrDefault(row.Category,"receipts");
                    if(section=="receipts"&&amount.CompareTo(UploadExactAmount.Zero)<0
                        ||section=="payments"&&amount.CompareTo(UploadExactAmount.Zero)>0)
                        Add(view,voucher.Key.VoucherId,voucher.Key.PeriodKey,"category_reversals",
                            "Refunds/reversals netted within "+section+" categories",row.Category,
                            UploadExactAmount.Zero-amount.Abs(),UploadExactAmount.Zero-amount.Abs());
                }
            }
        }
        return (adjustments,reviews);

        void Add(string reportView,string voucher,string period,string reason,string label,string category,
            UploadExactAmount receipts,UploadExactAmount payments)
        {
            if(receipts==UploadExactAmount.Zero&&payments==UploadExactAmount.Zero)return;
            adjustments.Add(new(reportView,voucher,period,reason,label,category,receipts.ToFixedString(),payments.ToFixedString()));
        }
        static UploadExactAmount Positive(UploadExactAmount value)=>value.CompareTo(UploadExactAmount.Zero)>0?value:UploadExactAmount.Zero;
        static UploadExactAmount NegativeAsPositive(UploadExactAmount value)=>value.CompareTo(UploadExactAmount.Zero)<0?UploadExactAmount.Zero-value:UploadExactAmount.Zero;
    }

    private sealed record LedgerOccurrence(string AccountCode,string Voucher,string Date,
        UploadExactAmount Debit,UploadExactAmount Credit,int RowId);
    private sealed record LoadedLedger(IReadOnlyList<LedgerOccurrence> Rows,IReadOnlySet<string> InvalidAccounts);
    private sealed record GrossSides(UploadExactAmount Receipts,UploadExactAmount Payments,bool Complete);

    private static IReadOnlyDictionary<string,object?> BuildWorkingPaper(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows,FinancialReportReconciliationResult reconciliation,
        string evidenceFingerprint,CancellationToken ct)
    {
        var diagnostics=new List<object>(reconciliation.Diagnostics);
        var accounts=snapshot.CashflowTreasuryAccounts.Items.GroupBy(row=>row.AccountCode,StringComparer.Ordinal)
            .ToDictionary(group=>group.Key,group=>group.Last().TreasuryType.Equals("cash",StringComparison.OrdinalIgnoreCase)?"Cash":"Bank",StringComparer.Ordinal);
        if(snapshot.Scope.SelectedAccountCodes is { } selected)accounts=accounts.Where(pair=>selected.Contains(pair.Key))
            .ToDictionary(pair=>pair.Key,pair=>pair.Value,StringComparer.Ordinal);
        var ledger=LoadLedgerOccurrences(snapshot,diagnostics,ct);
        string[] views=cashflows.TreasuryView=="split"?["combined","bank","cash"]:[cashflows.TreasuryView];
        var outputs=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(string view in views)
        {
            ct.ThrowIfCancellationRequested();
            outputs[view]=BuildWorkingPaperView(snapshot,cashflows,reconciliation,accounts,ledger,view,diagnostics,ct);
        }
        string cacheIdentity=HashIdentity(snapshot.Identity.Fingerprint,snapshot.Scope,cashflows.GroupBy,cashflows.TreasuryView);
        var root=new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["client_id"]=snapshot.Scope.ClientId,["template_id"]=snapshot.Scope.TemplateId,
            ["period_start"]=cashflows.Periods.FirstOrDefault()?.Start,["period_end"]=cashflows.Periods.LastOrDefault()?.End,
            ["group_by"]=cashflows.GroupBy,["treasury_view"]=cashflows.TreasuryView,
            ["receipt_presentation"]=cashflows.ReceiptPresentation,["periods"]=cashflows.Periods,
            ["views"]=outputs,["complete"]=false,["transaction_coverage_status"]="Not independently certified",
            ["scope_note"]="Presentation agreement and posting agreement are separate. Completeness of the original Transaction Report extraction is not independently certified.",
            ["diagnostics"]=diagnostics,["evidence_fingerprint"]=evidenceFingerprint,
            ["cache_schema"]=1,["cache_identity"]=cacheIdentity,["configuration_key"]=snapshot.Identity.ConfigurationFingerprint,
            ["report_revision"]=snapshot.CashflowReportRevision,
            ["account_selection"]=snapshot.Scope.SelectedAccountCodes is { } chosen
                ? new { version=1,mode="selected",account_codes=chosen,configured_account_codes=snapshot.CashflowTreasuryAccounts.Items
                    .Select(row=>row.AccountCode).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray() }
                : null
        };
        return root;
    }

    private static object BuildWorkingPaperView(ReportInputSnapshot snapshot,CashflowCalculationResult cashflows,
        FinancialReportReconciliationResult reconciliation,IReadOnlyDictionary<string,string> accountTypes,
        LoadedLedger ledger,string view,List<object> diagnostics,CancellationToken ct)
    {
        var selected=accountTypes.Where(pair=>view=="combined"||pair.Value.Equals(view,StringComparison.OrdinalIgnoreCase))
            .Select(pair=>pair.Key).ToHashSet(StringComparer.Ordinal);
        bool emptySelected=snapshot.Scope.SelectedAccountCodes is not null&&selected.Count==0;
        var accountRows=reconciliation.Accounts.Where(row=>selected.Contains(row.AccountCode)).ToArray();
        var proofByPeriod=cashflows.TreasuryMovementProof.Where(row=>row.TreasuryType==view)
            .ToDictionary(row=>row.PeriodKey,StringComparer.Ordinal);
        var grouped=GroupAdjustments(reconciliation.PresentationAdjustments.Where(row=>row.TreasuryView==view));
        var periods=new List<Dictionary<string,object?>>();
        var allSideTotals=new Dictionary<string,Dictionary<string,UploadExactAmount>>(StringComparer.Ordinal);
        var ledgerTotalsAvailable=new Dictionary<string,bool>(StringComparer.Ordinal);
        var adjustedTotalsAvailable=new Dictionary<string,bool>(StringComparer.Ordinal);
        var differenceTotalsAvailable=new Dictionary<string,bool>(StringComparer.Ordinal);
        foreach(string side in new[]{"receipts","payments"})
        {
            allSideTotals[side]=new(StringComparer.Ordinal);
            ledgerTotalsAvailable[side]=true;
            adjustedTotalsAvailable[side]=true;
            differenceTotalsAvailable[side]=true;
        }
        UploadExactAmount? firstOpening=null,lastClosing=null,totalTransfer=UploadExactAmount.Zero,totalMixed=UploadExactAmount.Zero;
        bool allBalancesAvailable=accountRows.Length>0;
        foreach(var period in cashflows.Periods)
        {
            ct.ThrowIfCancellationRequested();
            var accountPeriods=accountRows.Select(account=>account.Periods.FirstOrDefault(row=>row.PeriodKey==period.Key)).ToArray();
            bool balanceAvailable=accountRows.Length>0&&accountPeriods.Length==accountRows.Length
                &&accountPeriods.All(row=>row?.LedgerOpening is not null&&row.LedgerClosing is not null&&row.LedgerMovement is not null);
            allBalancesAvailable&=balanceAvailable;
            UploadExactAmount? opening=balanceAvailable?Sum(accountPeriods.Select(row=>UploadExactAmount.Parse(row!.LedgerOpening!))):null;
            UploadExactAmount? closing=balanceAvailable?Sum(accountPeriods.Select(row=>UploadExactAmount.Parse(row!.LedgerClosing!))):null;
            if(periods.Count==0)firstOpening=opening;lastClosing=closing;
            var ledgerGross=GrossForPeriod(ledger,selected,period.Key,period.Start,period.End);
            bool ledgerAvailable=balanceAvailable&&ledgerGross.Complete;
            var transactionGross=GrossForTransaction(cashflows.ReconciliationLines,selected,period.Key,period.Start,period.End);
            var cashflowSides=CashflowSides(cashflows,view,period.Key);
            var periodAdjustments=grouped.SelectMany(row=>row.Periods.Where(pair=>pair.Key==period.Key)
                    .Select(pair=>(Group:row,Values:pair.Value)))
                .ToArray();
            var sides=new Dictionary<string,object?>(StringComparer.Ordinal);
            foreach(string side in new[]{"receipts","payments"})
            {
                UploadExactAmount adjustment=periodAdjustments.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+item.Values[side]);
                UploadExactAmount? rawLedger=ledgerAvailable?(side=="receipts"?ledgerGross.Receipts:ledgerGross.Payments):null;
                UploadExactAmount rawTransaction=side=="receipts"?transactionGross.Receipts:transactionGross.Payments;
                UploadExactAmount cashflow=cashflowSides[side];
                UploadExactAmount? adjusted=rawLedger is { } raw?raw+adjustment:null;
                UploadExactAmount? difference=adjusted is { } adjustedSide?adjustedSide-cashflow:null;
                sides[side]=new Dictionary<string,object?>(StringComparer.Ordinal)
                {
                    ["ledger"]=LegacyNumber(rawLedger),["transaction"]=LegacyNumber(rawTransaction),
                    ["adjustments"]=LegacyNumber(adjustment),["adjusted_ledger"]=LegacyNumber(adjusted),
                    ["cashflow"]=LegacyNumber(cashflow),["difference"]=LegacyNumber(difference)
                };
                if(rawLedger is null)ledgerTotalsAvailable[side]=false;
                else allSideTotals[side]["ledger"]=allSideTotals[side].GetValueOrDefault("ledger")+rawLedger.Value;
                foreach(var (field,value) in new[]{("transaction",rawTransaction),("adjustments",adjustment),("cashflow",cashflow)})
                    allSideTotals[side][field]=allSideTotals[side].GetValueOrDefault(field)+value;
                if(adjusted is { } adjustedValue)
                    allSideTotals[side]["adjusted_ledger"]=allSideTotals[side].GetValueOrDefault("adjusted_ledger")+adjustedValue;
                else adjustedTotalsAvailable[side]=false;
                if(difference is { } differenceValue)
                    allSideTotals[side]["difference"]=allSideTotals[side].GetValueOrDefault("difference")+differenceValue;
                else differenceTotalsAvailable[side]=false;
            }
            proofByPeriod.TryGetValue(period.Key,out var movement);
            UploadExactAmount? separateTransfer=movement is null?null:UploadExactAmount.Parse(movement.InternalTransferMovement);
            UploadExactAmount? separateMixed=movement is null?null:UploadExactAmount.Parse(movement.MixedVoucherMovement);
            if(separateTransfer is { } st)totalTransfer+=st;
            if(separateMixed is { } sm)totalMixed+=sm;
            UploadExactAmount? calculated=opening is { } open&&separateTransfer is { } transfer&&separateMixed is { } mix
                ?open+cashflowSides["receipts"]-cashflowSides["payments"]+transfer+mix:null;
            bool periodUnavailable=sides.Values.Cast<Dictionary<string,object?>>().Any(side=>side["difference"] is null);
            bool periodDiffers=sides.Values.Cast<Dictionary<string,object?>>().Any(side=>side["difference"] is double amount&&amount!=0);
            periods.Add(new Dictionary<string,object?>(StringComparer.Ordinal)
            {
                ["key"]=period.Key,["start"]=period.Start,["end"]=period.End,
                ["receipts"]=sides["receipts"],["payments"]=sides["payments"],
                ["amount_status"]=periodUnavailable?"Unavailable":periodDiffers?"Differences":"Matched",
                ["balance_proof"]=new Dictionary<string,object?>(StringComparer.Ordinal)
                {
                    ["ledger_opening"]=LegacyNumber(opening),["ledger_closing"]=LegacyNumber(closing),
                    ["separate_transfer_movement"]=LegacyNumber(separateTransfer),["separate_mixed_movement"]=LegacyNumber(separateMixed),
                    ["unallocated_category_movement"]=movement is null?null:LegacyNumber(UploadExactAmount.Parse(movement.UnallocatedCategoryMovement)),
                    ["calculated_closing"]=LegacyNumber(calculated),
                    ["difference"]=closing is { } close&&calculated is { } calc?LegacyNumber(close-calc):null,
                    ["continuous_calculated_closing"]=null,["continuous_difference"]=null
                }
            });
        }
        var overall=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(string side in new[]{"receipts","payments"})
        {
            if(!ledgerTotalsAvailable[side])allSideTotals[side].Remove("ledger");
            if(!adjustedTotalsAvailable[side])allSideTotals[side].Remove("adjusted_ledger");
            if(!differenceTotalsAvailable[side])allSideTotals[side].Remove("difference");
            var fields=new Dictionary<string,object?>(StringComparer.Ordinal);
            foreach(string field in new[]{"ledger","transaction","adjustments","adjusted_ledger","cashflow","difference"})
                fields[field]=allSideTotals[side].TryGetValue(field,out var value)?LegacyNumber(value):null;
            overall[side]=fields;
        }
        var adjustmentRows=grouped.Select(group=>new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["key"]=group.Key,["reason"]=group.Reason,["label"]=group.Label,["category"]=group.Category,
            ["periods"]=group.Periods.ToDictionary(pair=>pair.Key,pair=>new Dictionary<string,object?>
                { ["receipts"]=LegacyNumber(pair.Value["receipts"]),["payments"]=LegacyNumber(pair.Value["payments"]) },StringComparer.Ordinal),
            ["total"]=new Dictionary<string,object?>
                { ["receipts"]=LegacyNumber(group.Totals["receipts"]),["payments"]=LegacyNumber(group.Totals["payments"]) },
            ["support_count"]=group.SupportCount
        }).ToArray();
        var adjustmentsBySection=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(string side in new[]{"receipts","payments"})
        {
            var add=UploadExactAmount.Zero;var less=UploadExactAmount.Zero;
            foreach(var group in grouped)
            {var value=group.Totals[side];if(value.CompareTo(UploadExactAmount.Zero)>0)add+=value;else less+=value;}
            adjustmentsBySection[side]=new Dictionary<string,object?>
                { ["add"]=LegacyNumber(add),["less"]=LegacyNumber(less) };
        }
        var balancePeriods=periods.Select(row=>new Dictionary<string,object?>
        { ["key"]=row["key"],["start"]=row["start"],["end"]=row["end"],
          ["complete"]=row["balance_proof"] is Dictionary<string,object?> bp&&bp["ledger_opening"] is not null&&bp["ledger_closing"] is not null }).ToArray();
        var postingDiagnostics=diagnostics.ToArray();
        var proof=periods.Select(row=>row["balance_proof"] as Dictionary<string,object?>).Where(row=>row is not null).ToArray();
        UploadExactAmount? totalNet=null;
        if(allSideTotals["receipts"].TryGetValue("cashflow",out var totalReceipts)
            &&allSideTotals["payments"].TryGetValue("cashflow",out var totalPayments))totalNet=totalReceipts-totalPayments;
        UploadExactAmount? calculatedClose=firstOpening is { } openingTotal&&totalNet is { } net
            ?openingTotal+net+totalTransfer+totalMixed:null;
        var firstProof=proof.FirstOrDefault();var lastProof=proof.LastOrDefault();
        var openingValue=firstProof?.GetValueOrDefault("ledger_opening");var closingValue=lastProof?.GetValueOrDefault("ledger_closing");
        double? Number(object? value)=>value is double number?number:null;
        double? finalClose=calculatedClose is { } final?LegacyNumber(final):null;
        double? ledgerClose=Number(closingValue);
        var overallProof=new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["ledger_opening"]=openingValue,["ledger_closing"]=closingValue,
            ["separate_transfer_movement"]=LegacyNumber(totalTransfer),["separate_mixed_movement"]=LegacyNumber(totalMixed),
            ["calculated_closing"]=finalClose,["difference"]=ledgerClose is { } lc&&finalClose is { } fc?lc-fc:null
        };
        string amountStatus=periods.Any(row=>row["receipts"] is Dictionary<string,object?> receipt&&receipt["difference"] is null
            ||row["payments"] is Dictionary<string,object?> payment&&payment["difference"] is null)?"Unavailable"
            :periods.Any(row=>row["receipts"] is Dictionary<string,object?> receipt&&receipt["difference"] is double rd&&rd!=0
                ||row["payments"] is Dictionary<string,object?> payment&&payment["difference"] is double pd&&pd!=0)?"Differences":"Matched";
        string status=amountStatus=="Matched"?"Review required":amountStatus;
        if(emptySelected)status="Not applicable";
        var startDate=DateOnly.ParseExact(cashflows.Periods[0].Start,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        var endDate=DateOnly.ParseExact(cashflows.Periods[^1].End,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        bool InRange(string value)=>DateOnly.TryParseExact(value,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var day)
            &&day>=startDate&&day<=endDate;
        var ledgerPopulation=ledger.Rows.Where(row=>selected.Contains(row.AccountCode)&&InRange(row.Date)).ToArray();
        var txPopulation=cashflows.ReconciliationLines.Where(row=>row.IsInPeriod&&row.IsTreasury&&selected.Contains(row.AccountCode)
            &&InRange(row.Date)).ToArray();
        var ledgerIds=ledgerPopulation.Select(row=>row.Voucher.Trim()).Where(value=>value.Length>0).ToHashSet(StringComparer.Ordinal);
        var transactionIds=txPopulation.Select(row=>row.VoucherId.Trim()).Where(value=>value.Length>0).ToHashSet(StringComparer.Ordinal);
        var cashflowIds=cashflows.ReconciliationLines.Where(row=>!row.IsTreasury&&row.CategoryOwned&&InRange(row.Date)
                &&(view=="combined"||row.CategoryTreasuryType.Equals(view,StringComparison.OrdinalIgnoreCase))
                &&(UploadExactAmount.Parse(row.Credit)-UploadExactAmount.Parse(row.Debit)).CompareTo(UploadExactAmount.Zero)!=0)
            .Select(row=>row.VoucherId).Where(value=>value.Length>0).ToHashSet(StringComparer.Ordinal);
        var supportRows=reconciliation.PresentationAdjustments.Where(row=>row.TreasuryView==view).ToArray();
        var supportGroups=new Dictionary<string,object?>(StringComparer.Ordinal);
        foreach(var group in grouped)
        {
            var members=supportRows.Where(row=>AdjustmentKey(row)==group.Key).ToArray();
            supportGroups[group.Key]=new Dictionary<string,object?>
            {
                ["vouchers"]=members.Select(row=>row.VoucherId).Distinct(StringComparer.Ordinal).Count(),
                ["receipts"]=members.Where(row=>UploadExactAmount.Parse(row.Receipts).CompareTo(UploadExactAmount.Zero)!=0)
                    .Select(row=>row.VoucherId).Distinct(StringComparer.Ordinal).Count(),
                ["payments"]=members.Where(row=>UploadExactAmount.Parse(row.Payments).CompareTo(UploadExactAmount.Zero)!=0)
                    .Select(row=>row.VoucherId).Distinct(StringComparer.Ordinal).Count()
            };
        }
        bool ledgerCountReady=allBalancesAvailable&&!selected.Any(ledger.InvalidAccounts.Contains);
        var completeness=new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["schema"]=1,
            ["ledger"]=new Dictionary<string,object?>
                { ["vouchers"]=ledgerCountReady?ledgerIds.Count:null,["blank_id_rows"]=ledgerCountReady?ledgerPopulation.Count(row=>string.IsNullOrWhiteSpace(row.Voucher)):null },
            ["transaction"]=new Dictionary<string,object?>
                { ["vouchers"]=transactionIds.Count,["blank_id_rows"]=txPopulation.Count(row=>string.IsNullOrWhiteSpace(row.VoucherId)) },
            ["cashflow"]=new Dictionary<string,object?>{ ["vouchers"]=cashflowIds.Count },
            ["adjustments"]=new Dictionary<string,object?>
                { ["vouchers"]=supportRows.Select(row=>row.VoucherId).Distinct(StringComparer.Ordinal).Count(),["groups"]=supportGroups },
            ["source_ids_match"]=ledgerCountReady?ledgerIds.SetEquals(transactionIds):null,
            ["ledger_only"]=ledgerCountReady?ledgerIds.Except(transactionIds).Count():null,
            ["transaction_only"]=ledgerCountReady?transactionIds.Except(ledgerIds).Count():null,
            ["outside_cashflow"]=transactionIds.Except(cashflowIds).Count(),
            ["cashflow_only"]=cashflowIds.Except(transactionIds).Count()
        };
        return new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["status"]=status,["amount_status"]=emptySelected?"Not applicable":amountStatus,
            ["periods"]=periods,["overall"]=overall,["adjustments"]=adjustmentRows,
            ["adjustment_sections"]=adjustmentsBySection,["overall_balance_proof"]=overallProof,
            ["allocation_status"]=cashflows.TreasuryReconciliation?.AllocationStatus??"Proven",
            ["unresolved"]=reconciliation.VoucherReviews.Where(row=>row.TreasuryView==view),
            ["balance_controls"]=new Dictionary<string,object?>
                { ["coverage_complete"]=allBalancesAvailable,["periods"]=balancePeriods,["accounts"]=accountRows },
            ["treasury_movement_proof"]=cashflows.TreasuryMovementProof.Where(row=>row.TreasuryType==view),
            ["posting_comparison"]=new Dictionary<string,object?>
                { ["posting_status"]=reconciliation.PostingStatus,["coverage_diagnostics"]=postingDiagnostics },
            ["voucher_completeness"]=completeness
        };
    }

    private sealed record WorkingPaperAdjustmentGroup(string Key,string Reason,string Label,string Category,
        Dictionary<string,Dictionary<string,UploadExactAmount>> Periods,Dictionary<string,UploadExactAmount> Totals,int SupportCount);

    private static IReadOnlyList<WorkingPaperAdjustmentGroup> GroupAdjustments(
        IEnumerable<FinancialReportPresentationAdjustment> rows)
    {
        var groups=new Dictionary<string,(string Reason,string Label,string Category,
            Dictionary<string,Dictionary<string,UploadExactAmount>> Periods,HashSet<(string,string)> Members)>(StringComparer.Ordinal);
        foreach(var row in rows)
        {
            string payload="["+QuoteStableString(row.TreasuryView)+", "+QuoteStableString(row.Reason)+", "+QuoteStableString(row.Label)+", "+QuoteStableString(row.Category)+"]";
            string key=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload))).ToLowerInvariant()[..24];
            if(!groups.TryGetValue(key,out var group))group=(row.Reason,row.Label,row.Category,new(StringComparer.Ordinal),new());
            if(!group.Periods.TryGetValue(row.PeriodKey,out var totals))group.Periods[row.PeriodKey]=totals=new(StringComparer.Ordinal)
                { ["receipts"]=UploadExactAmount.Zero,["payments"]=UploadExactAmount.Zero };
            totals["receipts"]+=UploadExactAmount.Parse(row.Receipts);totals["payments"]+=UploadExactAmount.Parse(row.Payments);
            group.Members.Add((row.VoucherId,row.PeriodKey));groups[key]=group;
        }
        return groups.Select(pair=>new WorkingPaperAdjustmentGroup(pair.Key,pair.Value.Reason,pair.Value.Label,pair.Value.Category,
            pair.Value.Periods,new Dictionary<string,UploadExactAmount>
            {
                ["receipts"]=pair.Value.Periods.Values.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+row["receipts"]),
                ["payments"]=pair.Value.Periods.Values.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+row["payments"])
            },pair.Value.Members.Count)).ToArray();
    }

    private static LoadedLedger LoadLedgerOccurrences(ReportInputSnapshot snapshot,List<object> diagnostics,CancellationToken ct)
    {
        var rows=new List<LedgerOccurrence>();var invalid=new HashSet<string>(StringComparer.Ordinal);
        var rowsByUpload=snapshot.CashflowLedgerEvidenceRows.Items.GroupBy(row=>row.UploadId)
            .ToDictionary(group=>group.Key,group=>group.ToArray());
        foreach(var upload in snapshot.CashflowLedgerEvidenceUploads.Items)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                using var rolesDoc=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");
                string RoleColumn(string role)
                {
                    if(!rolesDoc.RootElement.TryGetProperty(role,out var id))throw new FormatException();
                    string key=id.ToString();using var columnsDoc=JsonDocument.Parse(upload.TemplateColumnsJson);
                    foreach(var column in columnsDoc.RootElement.EnumerateArray())
                        if(column.TryGetProperty("id",out var found)&&found.ToString()==key
                            &&column.TryGetProperty("column_name",out var name))return name.GetString()??throw new FormatException();
                    throw new FormatException();
                }
                string debit=RoleColumn("debit"),credit=RoleColumn("credit");
                string? dateColumn=rolesDoc.RootElement.TryGetProperty("date",out _)?RoleColumn("date"):null;
                foreach(var row in rowsByUpload.GetValueOrDefault(upload.Id)??[])
                {
                    ct.ThrowIfCancellationRequested();using var data=JsonDocument.Parse(row.RowDataJson);
                    DateOnly date=row.ExtractDate??DateOnly.MinValue;
                    if(dateColumn is not null&&DateOnly.TryParse(Column(data.RootElement,dateColumn),CultureInfo.InvariantCulture,out var parsed))date=parsed;
                    string debitText=Column(data.RootElement,debit),creditText=Column(data.RootElement,credit);
                    if(date==DateOnly.MinValue||string.IsNullOrWhiteSpace(debitText)&&string.IsNullOrWhiteSpace(creditText))
                        throw new FormatException();
                    rows.Add(new(upload.AccountCode,Column(data.RootElement,RoleColumn("voucher")),
                        date.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),Amount(debitText),Amount(creditText),row.Id));
                }
            }
            catch(Exception error) when(error is JsonException or FormatException or InvalidOperationException)
            {
                invalid.Add(upload.AccountCode);
                diagnostics.Add($"Ledger gross-side evidence for account {upload.AccountCode} is incomplete or unreadable.");
            }
        }
        return new(rows,invalid);
    }

    private static GrossSides GrossForPeriod(LoadedLedger source,IReadOnlySet<string> codes,string key,string start,string end)
    {
        var rows=source.Rows.Where(row=>codes.Contains(row.AccountCode)&&DateOnly.TryParseExact(row.Date,"yyyy-MM-dd",
            CultureInfo.InvariantCulture,DateTimeStyles.None,out var date)&&date>=DateOnly.ParseExact(start,"yyyy-MM-dd",CultureInfo.InvariantCulture)
            &&date<=DateOnly.ParseExact(end,"yyyy-MM-dd",CultureInfo.InvariantCulture)).ToArray();
        UploadExactAmount receipts=UploadExactAmount.Zero,payments=UploadExactAmount.Zero;
        foreach(var row in rows)
        {receipts+=Positive(row.Debit)+NegativeAsPositive(row.Credit);payments+=Positive(row.Credit)+NegativeAsPositive(row.Debit);}
        return new(receipts,payments,!codes.Any(source.InvalidAccounts.Contains));
    }

    private static GrossSides GrossForTransaction(IReadOnlyList<CashflowReconciliationLine> rows,
        IReadOnlySet<string> codes,string key,string start,string end)
    {
        var scoped=rows.Where(row=>row.IsInPeriod&&row.IsTreasury&&codes.Contains(row.AccountCode)&&row.PeriodKey==key).ToArray();
        UploadExactAmount receipts=UploadExactAmount.Zero,payments=UploadExactAmount.Zero;
        foreach(var row in scoped)
        {var debit=UploadExactAmount.Parse(row.Debit);var credit=UploadExactAmount.Parse(row.Credit);
         receipts+=Positive(debit)+NegativeAsPositive(credit);payments+=Positive(credit)+NegativeAsPositive(debit);}
        return new(receipts,payments,true);
    }

    private static Dictionary<string,UploadExactAmount> CashflowSides(CashflowCalculationResult result,string view,string period)
    {
        var values=new Dictionary<string,UploadExactAmount>(StringComparer.Ordinal)
            { ["receipts"]=UploadExactAmount.Zero,["payments"]=UploadExactAmount.Zero };
        if(view is "bank" or "cash")
        {
            foreach(var row in result.TreasuryCategoryTotals.Where(row=>row.PeriodKey==period))
            {
                var amount=UploadExactAmount.Parse(view=="bank"?row.BankAmount:row.CashAmount);
                Add(amount);
            }
        }
        else foreach(var row in result.CategoryTotals)
            Add(UploadExactAmount.Parse(row.Periods.GetValueOrDefault(period,"0")));
        return values;
        void Add(UploadExactAmount amount)
        {if(amount.CompareTo(UploadExactAmount.Zero)>=0)values["receipts"]+=amount;else values["payments"]+=amount.Abs();}
    }

    private static UploadExactAmount Sum(IEnumerable<UploadExactAmount> values)=>values.Aggregate(UploadExactAmount.Zero,(total,value)=>total+value);
    private static UploadExactAmount Positive(UploadExactAmount value)=>value.CompareTo(UploadExactAmount.Zero)>0?value:UploadExactAmount.Zero;
    private static UploadExactAmount NegativeAsPositive(UploadExactAmount value)=>value.CompareTo(UploadExactAmount.Zero)<0?UploadExactAmount.Zero-value:UploadExactAmount.Zero;
    internal static string AdjustmentKey(FinancialReportPresentationAdjustment row)
    {
        string payload="["+QuoteStableString(row.TreasuryView)+", "+QuoteStableString(row.Reason)+", "+QuoteStableString(row.Label)+", "+QuoteStableString(row.Category)+"]";
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload))).ToLowerInvariant()[..24];
    }
    private static double? LegacyNumber(UploadExactAmount? amount)
        =>amount is { } value&&double.TryParse(value.ToFixedString(),NumberStyles.Float,CultureInfo.InvariantCulture,out var number)&&double.IsFinite(number)?number:null;
    private static string HashIdentity(string fingerprint,ReportInputScope scope,string group,string view)
        =>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(fingerprint+"|"+JsonSerializer.Serialize(scope)+"|"+group+"|"+view))).ToLowerInvariant();

    private static IReadOnlyList<FinancialReportPostingFinding> ComparePostings(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows, IReadOnlyDictionary<string,string> accountTypes,
        IReadOnlyList<FinancialReportAccountReconciliation> accountResults,
        List<string> diagnostics, CancellationToken ct)
    {
        var findings = new List<FinancialReportPostingFinding>();
        var transaction = cashflows.TreasuryPostings.GroupBy(row => (row.AccountCode,row.VoucherId))
            .ToDictionary(group => group.Key, group => group.Select(row => PostingKey(row.Date,row.Debit,row.Credit))
                .GroupBy(key=>key,StringComparer.Ordinal).ToDictionary(group=>group.Key,group=>group.Count(),StringComparer.Ordinal));
        var ledgerRows = new List<(string Code,string Voucher,string Date,string Debit,string Credit,
            int Id,int UploadId,int TemplateId,int Version,string? Fingerprint,string AccountName)>();
        foreach(var upload in snapshot.CashflowLedgerEvidenceUploads.Items.Where(row=>accountTypes.ContainsKey(row.AccountCode)))
        {
            try
            {
                using var doc=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");
                var roles=doc.RootElement;
                string? RoleColumn(string role)
                {
                    if(!roles.TryGetProperty(role,out var id)||id.ValueKind==JsonValueKind.Null)return null;
                    string key=id.ToString();using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
                    foreach(var column in columns.RootElement.EnumerateArray())
                        if(column.TryGetProperty("id",out var columnId)&&columnId.ToString()==key
                            &&column.TryGetProperty("column_name",out var name))return name.GetString();
                    return null;
                }
                var voucherColumn=RoleColumn("voucher");var dateColumn=RoleColumn("date");
                var debitColumn=RoleColumn("debit");var creditColumn=RoleColumn("credit");
                if(voucherColumn is null||dateColumn is null||debitColumn is null||creditColumn is null)
                {diagnostics.Add($"Ledger posting identity is incomplete for account {upload.AccountCode}.");continue;}
                foreach(var row in snapshot.CashflowLedgerEvidenceRows.Items.Where(row=>row.UploadId==upload.Id))
                {
                    using var json=JsonDocument.Parse(row.RowDataJson);var root=json.RootElement;
                    string voucher=Column(root,voucherColumn).Trim();string dateText=Column(root,dateColumn);
                    if(voucher.Length==0||!DateOnly.TryParse(dateText,CultureInfo.InvariantCulture,out var day))
                    {ledgerRows.Add((upload.AccountCode,voucher,"!invalid",Column(root,debitColumn),Column(root,creditColumn),
                        row.Id,row.UploadId,row.TemplateId,row.Version,row.SourceFingerprint,upload.AccountName??upload.AccountCode));continue;}
                    ledgerRows.Add((upload.AccountCode,voucher,day.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture),
                        Column(root,debitColumn),Column(root,creditColumn),row.Id,row.UploadId,row.TemplateId,row.Version,
                        row.SourceFingerprint,upload.AccountName??upload.AccountCode));
                }
            }
            catch(JsonException){diagnostics.Add($"Ledger posting rows for account {upload.AccountCode} cannot be read.");}
        }
        var ledger=ledgerRows.GroupBy(row=>(row.Code,row.Voucher)).ToDictionary(group=>group.Key,
            group=>group.Select(row=>PostingKey(row.Date,row.Debit,row.Credit)).GroupBy(key=>key,StringComparer.Ordinal)
                .ToDictionary(group=>group.Key,group=>group.Count(),StringComparer.Ordinal));
        foreach(var key in transaction.Keys.Union(ledger.Keys).OrderBy(key=>key.Item1,StringComparer.Ordinal).ThenBy(key=>key.Item2,StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();var left=ledger.GetValueOrDefault(key)??new(StringComparer.Ordinal);
            var right=transaction.GetValueOrDefault(key)??new(StringComparer.Ordinal);
            bool equal=left.Count==right.Count&&left.All(pair=>right.GetValueOrDefault(pair.Key)==pair.Value);
            bool coverageUnavailable=accountResults.FirstOrDefault(row=>row.AccountCode==key.Item1)?.Periods
                .Any(period=>period.Status=="Unavailable")??true;
            string status=coverageUnavailable?"unavailable":key.Item2.Length==0||left.ContainsKey("!invalid")||right.ContainsKey("!invalid")?"unresolved":equal?"matched":left.Count==0?"transaction_only":right.Count==0?"ledger_only":"changed";
            var leftRows=ledgerRows.Where(row=>row.Code==key.Item1&&row.Voucher==key.Item2).ToArray();
            var rightRows=cashflows.TreasuryPostings.Where(row=>row.AccountCode==key.Item1&&row.VoucherId==key.Item2).ToArray();
            var fields=new List<string>();
            if(!equal)
            {
                if(left.Keys.Select(value=>value.Split('|')[0]).Order(StringComparer.Ordinal)
                    .SequenceEqual(right.Keys.Select(value=>value.Split('|')[0]).Order(StringComparer.Ordinal)) is false)fields.Add("dates");
                if(!left.OrderBy(pair=>pair.Key,StringComparer.Ordinal).SequenceEqual(right.OrderBy(pair=>pair.Key,StringComparer.Ordinal)))fields.Add("amounts");
                if(left.Values.Sum()!=right.Values.Sum())fields.Add("line_count");
            }
            UploadExactAmount? Movement(IEnumerable<(string Date,string Debit,string Credit)> rows)
            {
                try{return rows.Where(row=>DateOnly.TryParseExact(row.Date,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,
                        out var day)&&cashflows.Periods.Count>0&&day>=DateOnly.ParseExact(cashflows.Periods[0].Start,"yyyy-MM-dd",CultureInfo.InvariantCulture)
                        &&day<=DateOnly.ParseExact(cashflows.Periods[^1].End,"yyyy-MM-dd",CultureInfo.InvariantCulture))
                    .Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+Amount(row.Debit)-Amount(row.Credit));}
                catch(FormatException){return null;}
            }
            var ledgerMove=Movement(leftRows.Select(row=>(row.Date,row.Debit,row.Credit)));
            var transactionMove=Movement(rightRows.Select(row=>(row.Date,row.Debit,row.Credit)));
            var sourceRows=snapshot.ClientData.Items.ToDictionary(row=>row.Id);
            var ledgerEvidence=leftRows.Select(row=>(object)new Dictionary<string,object?>
            { ["id"]=row.Id,["upload_id"]=row.UploadId,["template_id"]=row.TemplateId,["version"]=row.Version,
              ["fingerprint"]=row.Fingerprint,["date"]=row.Date,["account_code"]=row.Code,["account_name"]=row.AccountName,
              ["voucher_id"]=row.Voucher,["debit"]=row.Debit,["credit"]=row.Credit,["in_period"]=IsInReport(row.Date,cashflows) }).ToArray();
            var transactionEvidence=rightRows.Select(row=>(object)new Dictionary<string,object?>
            { ["id"]=row.SourceRowId,["date"]=row.Date,["account_code"]=row.AccountCode,["voucher_id"]=row.VoucherId,
              ["debit"]=row.Debit,["credit"]=row.Credit,["in_period"]=true,
              ["upload_id"]=sourceRows.GetValueOrDefault(row.SourceRowId)?.UploadId,
              ["template_id"]=sourceRows.GetValueOrDefault(row.SourceRowId)?.TemplateId,
              ["version"]=sourceRows.GetValueOrDefault(row.SourceRowId)?.Version,
              ["fingerprint"]=sourceRows.GetValueOrDefault(row.SourceRowId)?.SourceFingerprint,
              ["row_data"]=sourceRows.GetValueOrDefault(row.SourceRowId)?.RowDataJson }).ToArray();
            var voucherEvidence=cashflows.ReconciliationLines.Where(row=>row.VoucherId==key.Item2).Select(row=>(object)new Dictionary<string,object?>
            { ["id"]=row.SourceRowId,["date"]=row.Date,["account_code"]=row.AccountCode,["account_name"]=row.AccountName,
              ["voucher_id"]=row.VoucherId,["debit"]=row.Debit,["credit"]=row.Credit,["description"]=row.Category,["in_period"]=row.IsInPeriod }).ToArray();
            string note=status switch
            {"matched"=>"The current ledger and Transaction Report postings agree by date and gross debit/credit amounts.",
             "transaction_only"=>"Current Transaction Report postings have no matching current ledger reference.",
             "ledger_only"=>"Current ledger postings have no matching Transaction Report reference.",
             "unavailable"=>"Missing coverage or current ledger configuration means absence cannot be established.",
             "unresolved"=>"Blank or invalid reference/date/amount prevents a reliable comparison.",
             _=>"Current postings differ by date, gross debit/credit amounts, or line count."};
            findings.Add(new FinancialReportPostingFinding(key.Item1,accountTypes.GetValueOrDefault(key.Item1,"Bank"),key.Item2,status,
                left.Values.Sum(),right.Values.Sum(),fields)
            {
                LedgerMovement=LegacyNumber(ledgerMove),TransactionMovement=LegacyNumber(transactionMove),
                MovementDifference=ledgerMove is{} lm&&transactionMove is{} tm?LegacyNumber(lm-tm):null,
                Note=note,LedgerLines=ledgerEvidence,TransactionLines=transactionEvidence,VoucherLines=voucherEvidence
            });
        }
        return findings;

        static bool IsInReport(string date,CashflowCalculationResult result)
            =>DateOnly.TryParseExact(date,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var day)
                &&result.Periods.Count>0&&day>=DateOnly.ParseExact(result.Periods[0].Start,"yyyy-MM-dd",CultureInfo.InvariantCulture)
                &&day<=DateOnly.ParseExact(result.Periods[^1].End,"yyyy-MM-dd",CultureInfo.InvariantCulture);
    }

    private static IReadOnlyList<LedgerLine> ParseLines(ReportCashflowLedgerEvidenceUpload upload,
        IReadOnlyList<ReportCashflowLedgerEvidenceRow> rows)
    {
        using var rolesDoc=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");var roles=rolesDoc.RootElement;
        string ColumnName(string role)
        {
            if(!roles.TryGetProperty(role,out var id))throw new FormatException();string key=id.ToString();
            using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
            foreach(var col in columns.RootElement.EnumerateArray())
                if(col.TryGetProperty("id",out var found)&&found.ToString()==key&&col.TryGetProperty("column_name",out var name))return name.GetString()??throw new FormatException();
            throw new FormatException();
        }
        string debit=ColumnName("debit"),credit=ColumnName("credit");
        string? dateRole=roles.TryGetProperty("date",out _)?ColumnName("date"):null;
        var result=new List<LedgerLine>();
        foreach(var row in rows)
        {
            if(row.ExtractDate is null)throw new FormatException();using var data=JsonDocument.Parse(row.RowDataJson);
            var day=dateRole is null?row.ExtractDate.Value:ParseDate(Column(data.RootElement,dateRole),row.ExtractDate.Value);
            string dr=Column(data.RootElement,debit),cr=Column(data.RootElement,credit);
            if(string.IsNullOrWhiteSpace(dr)&&string.IsNullOrWhiteSpace(cr))throw new FormatException();
            result.Add(new(row.Id,"",day,Amount(dr),Amount(cr),row.Version,row.SourceFingerprint));
        }
        return result;
    }

    private static bool ConfigurationCurrent(ReportCashflowLedgerEvidenceUpload upload,JsonElement check)
    {
        if(!TryString(check,"configuration",out var expected))return false;
        try
        {
            using var roles=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
            string payload="["+CanonicalJson(roles.RootElement)+", "+CanonicalJson(columns.RootElement)+"]";
            string actual=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(payload))).ToLowerInvariant();
            return StringComparer.OrdinalIgnoreCase.Equals(actual,expected);
        }
        catch(JsonException){return false;}
    }

    private static string CanonicalJson(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Object=>"{"+string.Join(", ",value.EnumerateObject().OrderBy(p=>p.Name,StringComparer.Ordinal).Select(p=>QuoteStableString(p.Name)+": "+CanonicalJson(p.Value)))+"}",
        JsonValueKind.Array=>"["+string.Join(", ",value.EnumerateArray().Select(CanonicalJson))+"]",
        JsonValueKind.String=>QuoteStableString(value.GetString()??""),JsonValueKind.Number=>value.GetRawText(),
        JsonValueKind.True=>"true",JsonValueKind.False=>"false",_=>"null"
    };
    private static string QuoteStableString(string value)
    {
        var output=new StringBuilder("\"");foreach(char c in value)
        {if(c=='"')output.Append("\\\"");else if(c=='\\')output.Append("\\\\");else if(c=='\b')output.Append("\\b");else if(c=='\f')output.Append("\\f");else if(c=='\n')output.Append("\\n");else if(c=='\r')output.Append("\\r");else if(c=='\t')output.Append("\\t");else if(c<0x20||c>0x7e)output.Append("\\u").Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));else output.Append(c);}output.Append('"');return output.ToString();
    }
    private static string CanonicalFingerprint(IEnumerable<LedgerLine> rows)
    {
        var list=rows.OrderBy(row=>row.Id).ToArray();if(list.Any(row=>row.Fingerprint is null))return "";
        string text=string.Join(',',list.Select(row=>$"{row.Id}:{row.Version}:{row.Fingerprint}"));
        return Convert.ToHexString(MD5.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    }
    private static UploadExactAmount Sum(IEnumerable<LedgerLine> rows)=>rows.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+row.Debit-row.Credit);
    private static UploadExactAmount Amount(string value)=>string.IsNullOrWhiteSpace(value)?UploadExactAmount.Zero:UploadExactAmount.Parse(value);
    private static bool TryAmount(JsonElement root,string key,out UploadExactAmount value){value=default;if(!root.TryGetProperty(key,out var item)||item.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)return false;try{value=Amount(item.ToString());return true;}catch(FormatException){return false;}}
    private static bool TryString(JsonElement root,string key,out string value){value="";if(!root.TryGetProperty(key,out var item)||item.ValueKind!=JsonValueKind.String)return false;value=item.GetString()??"";return value.Length>0;}
    private static string Column(JsonElement row,string name){foreach(var p in row.EnumerateObject())if(StringComparer.OrdinalIgnoreCase.Equals(p.Name,name))return p.Value.ValueKind==JsonValueKind.String?p.Value.GetString()??"":p.Value.ValueKind==JsonValueKind.Null?"":p.Value.GetRawText();return "";}
    private static DateOnly ParseDate(string value,DateOnly fallback)=>DateOnly.TryParse(value,CultureInfo.InvariantCulture,out var result)?result:fallback;
    private static bool TryDate(DateOnly? value,out DateOnly result){result=value.GetValueOrDefault();return value.HasValue;}
    private static bool TryDate(string value,out DateOnly result)=>DateOnly.TryParseExact(value,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out result);
    private static string PostingKey(string date,string debit,string credit)=>$"{date}|{Amount(debit).ToFixedString()}|{Amount(credit).ToFixedString()}";

    internal static IReadOnlyDictionary<string,object?> AdjustmentTransactions(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows,FinancialReportReconciliationResult reconciliation,string supportKey,
        string side,string sourceBook,string search,int page,int limit)
    {
        if(side is not ("receipts" or "payments")||sourceBook is not ("ledger" or "voucher")||page<1||limit is <1 or >200)
            throw new FinancialStatementInputException("Invalid adjustment evidence scope.");
        var matching=reconciliation.PresentationAdjustments.Where(row=>AdjustmentKey(row)==supportKey
            &&UploadExactAmount.Parse(side=="receipts"?row.Receipts:row.Payments).CompareTo(UploadExactAmount.Zero)!=0).ToArray();
        if(matching.Length==0)throw new FinancialStatementInputException("Unknown or empty reconciliation adjustment for this side.");
        var proof=matching[0].TreasuryView;var members=matching.Select(row=>(row.VoucherId,row.PeriodKey)).Distinct().ToArray();
        var rows=AdjustmentEvidenceRows(snapshot,cashflows,proof,sourceBook,members);
        if(!string.IsNullOrWhiteSpace(search))rows=rows.Where(row=>
        {
            string json=JsonSerializer.Serialize(row);string code=row.GetValueOrDefault("account_code")?.ToString()??"";
            string name=row.GetValueOrDefault("account_name")?.ToString()??"";
            return json.Contains(search,StringComparison.OrdinalIgnoreCase)||code.Contains(search,StringComparison.OrdinalIgnoreCase)
                ||name.Contains(search,StringComparison.OrdinalIgnoreCase);
        }).ToList();
        int total=rows.Count,pageCount=Math.Max(1,(int)Math.Ceiling(total/(double)limit)),current=Math.Min(page,pageCount);
        var selected=rows.Skip((current-1)*limit).Take(limit).ToArray();
        var rawColumns=selected.Select(row=>row.GetValueOrDefault("row_data") as IReadOnlyDictionary<string,object?>)
            .Where(row=>row is not null).SelectMany(row=>row!.Keys).Distinct(StringComparer.Ordinal).ToArray();
        var amount=matching.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+UploadExactAmount.Parse(side=="receipts"?row.Receipts:row.Payments));
        return new Dictionary<string,object?>(StringComparer.Ordinal)
        {
            ["support_key"]=supportKey,["side"]=side,["proof"]=proof,
            ["adjustment_total"]=LegacyNumber(amount),["label"]=matching[0].Label,["category"]=matching[0].Category,
            ["transactions"]=new Dictionary<string,object?>
            { ["rows"]=selected,["columns"]=rawColumns,["total_rows"]=total,["page"]=current,["page_count"]=pageCount,
              ["limit"]=limit,["source_book"]=sourceBook,["search"]=search,["total_voucher_periods"]=members.Length }
        };
    }

    internal static IReadOnlyDictionary<string,object?> AdjustmentBundle(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows,FinancialReportReconciliationResult reconciliation,string proof,
        string sourceBook,string supportKey,string side)
    {
        if(sourceBook is not ("ledger" or "voucher")||proof is not ("combined" or "bank" or "cash"))
            throw new FinancialStatementInputException("Invalid adjustment bundle scope.");
        var candidates=reconciliation.PresentationAdjustments.Where(row=>row.TreasuryView==proof
            &&(string.IsNullOrEmpty(supportKey)||AdjustmentKey(row)==supportKey)
            &&(string.IsNullOrEmpty(side)
                ||UploadExactAmount.Parse(side=="receipts"?row.Receipts:row.Payments).CompareTo(UploadExactAmount.Zero)!=0)).ToArray();
        var memberships=candidates.Select(row=>(row.VoucherId,row.PeriodKey)).Distinct().ToArray();
        var rows=AdjustmentEvidenceRows(snapshot,cashflows,proof,sourceBook,memberships);
        var groupList=new List<Dictionary<string,object?>>();
        foreach(var group in candidates.GroupBy(AdjustmentKey,StringComparer.Ordinal))
        foreach(string groupSide in new[]{"receipts","payments"})
        {
            var membersForSide=group.Where(row=>UploadExactAmount.Parse(groupSide=="receipts"?row.Receipts:row.Payments)
                .CompareTo(UploadExactAmount.Zero)!=0).ToArray();
            if(membersForSide.Length==0)continue;
            var voucherSet=membersForSide.Select(row=>row.VoucherId).ToHashSet(StringComparer.Ordinal);
            groupList.Add(new Dictionary<string,object?>
            {
                ["key"]=group.Key,["side"]=groupSide,["label"]=membersForSide[0].Label,["category"]=membersForSide[0].Category,
                ["adjustment_total"]=LegacyNumber(membersForSide.Aggregate(UploadExactAmount.Zero,
                    (sum,row)=>sum+UploadExactAmount.Parse(groupSide=="receipts"?row.Receipts:row.Payments))),
                ["row_ids"]=rows.Where(row=>voucherSet.Contains(row.GetValueOrDefault("voucher_id")?.ToString()??""))
                    .Select(row=>row.GetValueOrDefault("id")).ToArray(),
                ["in_period_row_ids"]=rows.Where(row=>voucherSet.Contains(row.GetValueOrDefault("voucher_id")?.ToString()??"")
                    &&row.GetValueOrDefault("in_period") is true).Select(row=>row.GetValueOrDefault("id")).ToArray()
            });
        }
        var groups=groupList.ToArray();
        foreach(var row in rows)
        {
            string voucher=row.GetValueOrDefault("voucher_id")?.ToString()??"";
            var linked=groups.Where(group=>(group.GetValueOrDefault("row_ids") as object[])?.Any(id=>
                Convert.ToString(id,CultureInfo.InvariantCulture)==Convert.ToString(row.GetValueOrDefault("id"),CultureInfo.InvariantCulture))==true).ToArray();
            row["adjustment_groups"]=linked.Select(group=>$"{group["key"]}:{group["side"]}").ToArray();
            row["in_period_groups"]=linked.Where(_=>row.GetValueOrDefault("in_period") is true)
                .Select(group=>$"{group["key"]}:{group["side"]}").ToArray();
            row["search_text"]=JsonSerializer.Serialize(row.GetValueOrDefault("row_data"));
        }
        var columns=rows.Select(row=>row.GetValueOrDefault("row_data") as IReadOnlyDictionary<string,object?>)
            .Where(row=>row is not null).SelectMany(row=>row!.Keys).Distinct(StringComparer.Ordinal).ToArray();
        var result=new Dictionary<string,object?>(StringComparer.Ordinal)
        { ["bundle_schema"]=string.IsNullOrEmpty(supportKey)?1:2,["complete"]=true,["proof"]=proof,["source_book"]=sourceBook,
          ["rows"]=rows,["columns"]=columns,["groups"]=groups };
        if(rows.Count>1000||JsonSerializer.SerializeToUtf8Bytes(result).Length>1024*1024)
            return new Dictionary<string,object?>(StringComparer.Ordinal)
            { ["bundle_schema"]=string.IsNullOrEmpty(supportKey)?1:2,["complete"]=false,["reason"]=rows.Count>1000?"row_limit":"byte_limit",
              ["proof"]=proof,["source_book"]=sourceBook,["rows"]=Array.Empty<object>(),["columns"]=Array.Empty<string>(),["groups"]=Array.Empty<object>() };
        return result;
    }

    internal static List<Dictionary<string,object?>> AdjustmentEvidenceRows(ReportInputSnapshot snapshot,
        CashflowCalculationResult cashflows,string proof,string sourceBook,IReadOnlyList<(string VoucherId,string PeriodKey)> members)
    {
        var bounds=cashflows.Periods.ToDictionary(row=>row.Key,row=>(DateOnly.ParseExact(row.Start,"yyyy-MM-dd",CultureInfo.InvariantCulture),
            DateOnly.ParseExact(row.End,"yyyy-MM-dd",CultureInfo.InvariantCulture)),StringComparer.Ordinal);
        var memberKeys=members.ToHashSet();var vouchers=members.Select(row=>row.VoucherId).ToHashSet(StringComparer.Ordinal);
        var output=new List<Dictionary<string,object?>>();
        if(sourceBook=="voucher")
        {
            var sourceRows=snapshot.ClientData.Items.ToDictionary(row=>row.Id);
            foreach(var line in cashflows.ReconciliationLines.Where(row=>vouchers.Contains(row.VoucherId)))
            {
                if(!sourceRows.TryGetValue(line.SourceRowId,out var source))continue;
                using var doc=JsonDocument.Parse(source.RowDataJson);var raw=EvidenceObject(doc.RootElement) as Dictionary<string,object?>??new();
                DateOnly.TryParseExact(line.Date,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var date);
                bool inPeriod=members.Any(member=>member.VoucherId==line.VoucherId&&bounds.TryGetValue(member.PeriodKey,out var period)
                    &&date>=period.Item1&&date<=period.Item2);
                output.Add(EvidenceRow(line.SourceRowId,source.UploadId,source.TemplateId,source.Version,source.SourceFingerprint,
                    line.Date,line.VoucherId,line.AccountCode,line.AccountName,line.Debit,line.Credit,line.Category,raw,inPeriod));
            }
        }
        else
        {
            var uploads=snapshot.CashflowLedgerEvidenceUploads.Items.ToDictionary(row=>row.Id);
            foreach(var source in snapshot.CashflowLedgerEvidenceRows.Items.Where(row=>uploads.ContainsKey(row.UploadId)))
            {
                var upload=uploads[source.UploadId];
                using var roles=JsonDocument.Parse(upload.LedgerColumnsJson??"{}");using var columns=JsonDocument.Parse(upload.TemplateColumnsJson);
                string? ColumnFor(string role)
                {
                    if(!roles.RootElement.TryGetProperty(role,out var id))return null;string key=id.ToString();
                    foreach(var column in columns.RootElement.EnumerateArray())
                        if(column.TryGetProperty("id",out var found)&&found.ToString()==key&&column.TryGetProperty("column_name",out var name))return name.GetString();
                    return null;
                }
                var voucherColumn=ColumnFor("voucher");if(voucherColumn is null)continue;
                using var doc=JsonDocument.Parse(source.RowDataJson);var raw=EvidenceObject(doc.RootElement) as Dictionary<string,object?>??new();
                string voucher=Column(doc.RootElement,voucherColumn).Trim();if(!vouchers.Contains(voucher))continue;
                string debit=ColumnFor("debit") is{} dc?Column(doc.RootElement,dc):"";
                string credit=ColumnFor("credit") is{} cc?Column(doc.RootElement,cc):"";
                string date=source.ExtractDate?.ToString("yyyy-MM-dd",CultureInfo.InvariantCulture)??"";
                DateOnly.TryParseExact(date,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out var day);
                bool inPeriod=members.Any(member=>member.VoucherId==voucher&&bounds.TryGetValue(member.PeriodKey,out var period)
                    &&day>=period.Item1&&day<=period.Item2);
                var accountType=snapshot.CashflowTreasuryAccounts.Items.LastOrDefault(row=>row.AccountCode==upload.AccountCode)?.TreasuryType??"Bank";
                if(proof!="combined"&&!accountType.Equals(proof,StringComparison.OrdinalIgnoreCase))continue;
                output.Add(EvidenceRow(source.Id,source.UploadId,source.TemplateId,source.Version,source.SourceFingerprint,
                    date,voucher,upload.AccountCode,upload.AccountName??upload.AccountCode,debit,credit,"",raw,inPeriod));
            }
        }
        return output.OrderBy(row=>row.GetValueOrDefault("date")?.ToString(),StringComparer.Ordinal)
            .ThenBy(row=>Convert.ToInt32(row.GetValueOrDefault("id"),CultureInfo.InvariantCulture)).ToList();
    }

    private static Dictionary<string,object?> EvidenceRow(int id,int uploadId,int templateId,int version,string? fingerprint,
        string date,string voucher,string account,string name,string debit,string credit,string description,
        Dictionary<string,object?> raw,bool inPeriod)
    {
        var numeric=raw.Where(pair=>pair.Value is JsonNumberText).Select(pair=>pair.Key).ToArray();
        var converted=raw.ToDictionary(pair=>pair.Key,pair=>pair.Value is JsonNumberText number?number.Value:pair.Value,StringComparer.Ordinal);
        return new Dictionary<string,object?>
        { ["id"]=id,["upload_id"]=uploadId,["template_id"]=templateId,["version"]=version,["fingerprint"]=fingerprint,
          ["date"]=date,["voucher_id"]=voucher,["account_code"]=account,["account_name"]=name,
          ["debit"]=debit,["credit"]=credit,["description"]=description,["in_period"]=inPeriod,
          ["row_data"]=converted,["numeric_columns"]=numeric }
        ;
    }

    private sealed record JsonNumberText(string Value);
    private static object? EvidenceObject(JsonElement value)=>value.ValueKind switch
    {
        JsonValueKind.Object=>value.EnumerateObject().ToDictionary(property=>property.Name,property=>EvidenceObject(property.Value),StringComparer.Ordinal),
        JsonValueKind.Array=>value.EnumerateArray().Select(EvidenceObject).ToArray(),
        JsonValueKind.Number=>new JsonNumberText(value.GetRawText()),JsonValueKind.String=>value.GetString(),
        JsonValueKind.True=>true,JsonValueKind.False=>false,_=>null
    };
}
