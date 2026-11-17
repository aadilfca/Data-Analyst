using System.IO.Compression;
using System.Text;
using System.Xml;
using System.Globalization;
using System.Text.Json;
using AuditPlatform.Api.Cashflows;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.Reports;

internal sealed record FinancialReportWorkbookSheet(string Name,
    IReadOnlyList<IReadOnlyList<string?>> Rows);
internal sealed record CashflowWorkbookFilter(string Search = "", string Category = "",
    string AccountHead = "", string TreasuryType = "", string PeriodKey = "",
    string SummaryFilter = "", bool HideZeroRows = false, string TreasuryView = "combined",
    IReadOnlyList<string>? SelectedAccountCodes = null);

/// <summary>Bounded OOXML export using literal text cells and no spreadsheet formulas.</summary>
internal static class FinancialReportWorkbookWriter
{
    private const string MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    private const string RelNs = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    private const string PackageNs = "http://schemas.openxmlformats.org/package/2006/relationships";

    public static byte[] Write(IReadOnlyList<FinancialReportWorkbookSheet> sheets,
        ReportInputOptions options, CancellationToken ct = default)
    {
        if (!options.Valid() || sheets.Count is < 1 or > 16
            || sheets.Sum(sheet => sheet.Rows.Count) > options.MaximumWorkbookRows)
            throw new InvalidDataException("Financial report workbook exceeds its configured bounds.");
        if (sheets.Select(sheet => sheet.Name).Distinct(StringComparer.Ordinal).Count() != sheets.Count
            || sheets.Any(sheet => !ValidSheetName(sheet.Name)
                || sheet.Rows.Any(row => row.Count > 16384)))
            throw new InvalidDataException("Financial report workbook has an invalid sheet layout.");

        using var output = new CappedStream(options.MaximumWorkbookBytes);
        using (var zip = new ZipArchive(output, ZipArchiveMode.Create, true, Encoding.UTF8))
        {
            WriteEntry(zip,"[Content_Types].xml",writer =>
            {
                writer.WriteStartElement("Types","http://schemas.openxmlformats.org/package/2006/content-types");
                writer.WriteStartElement("Default");writer.WriteAttributeString("Extension","rels");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-package.relationships+xml");writer.WriteEndElement();
                writer.WriteStartElement("Default");writer.WriteAttributeString("Extension","xml");writer.WriteAttributeString("ContentType","application/xml");writer.WriteEndElement();
                writer.WriteStartElement("Override");writer.WriteAttributeString("PartName","/xl/workbook.xml");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");writer.WriteEndElement();
                writer.WriteStartElement("Override");writer.WriteAttributeString("PartName","/xl/styles.xml");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml");writer.WriteEndElement();
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("Override");writer.WriteAttributeString("PartName",$"/xl/worksheets/sheet{i+1}.xml");writer.WriteAttributeString("ContentType","application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");writer.WriteEndElement();}
                writer.WriteEndElement();
            });
            WriteEntry(zip,"_rels/.rels",writer=>
            {
                writer.WriteStartElement("Relationships",PackageNs);writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id","rId1");writer.WriteAttributeString("Type",RelNs+"/officeDocument");
                writer.WriteAttributeString("Target","xl/workbook.xml");writer.WriteEndElement();writer.WriteEndElement();
            });
            WriteEntry(zip,"xl/workbook.xml",writer=>
            {
                writer.WriteStartElement("workbook",MainNs);writer.WriteAttributeString("xmlns","r",null,RelNs);
                writer.WriteStartElement("sheets");
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("sheet");writer.WriteAttributeString("name",sheets[i].Name);writer.WriteAttributeString("sheetId",(i+1).ToString());writer.WriteAttributeString("r","id",RelNs,$"rId{i+1}");writer.WriteEndElement();}
                writer.WriteEndElement();writer.WriteEndElement();
            });
            WriteEntry(zip,"xl/_rels/workbook.xml.rels",writer=>
            {
                writer.WriteStartElement("Relationships",PackageNs);
                for(int i=0;i<sheets.Count;i++){writer.WriteStartElement("Relationship");writer.WriteAttributeString("Id",$"rId{i+1}");writer.WriteAttributeString("Type",RelNs+"/worksheet");writer.WriteAttributeString("Target",$"worksheets/sheet{i+1}.xml");writer.WriteEndElement();}
                writer.WriteStartElement("Relationship");writer.WriteAttributeString("Id",$"rId{sheets.Count+1}");writer.WriteAttributeString("Type",RelNs+"/styles");writer.WriteAttributeString("Target","styles.xml");writer.WriteEndElement();
                writer.WriteEndElement();
            });
            WriteEntry(zip,"xl/styles.xml",WriteStyles);
            for(int i=0;i<sheets.Count;i++)
            {
                ct.ThrowIfCancellationRequested();var sheet=sheets[i];
                WriteEntry(zip,$"xl/worksheets/sheet{i+1}.xml",writer=>WriteSheet(writer,sheet,options,ct));
            }
        }
        return output.ToArray();
    }

    public static IReadOnlyList<FinancialReportWorkbookSheet> CashflowSheets(
        CashflowCalculationResult result, FinancialReportReconciliationResult reconciliation,
        CashflowWorkbookFilter? filter = null,bool includeReconciliation=false,ReportInputSnapshot? snapshot=null)
    {
        filter ??= new CashflowWorkbookFilter();
        bool Contains(string value, string needle) => string.IsNullOrWhiteSpace(needle)
            || value.Contains(needle.Trim(), StringComparison.OrdinalIgnoreCase);
        var visibleCategories = result.CategoryTotals.Where(row => Contains(row.Category, filter.SummaryFilter)
            && (!filter.HideZeroRows || row.Periods.Values.Any(value => UploadExactAmount.Parse(value)
                .CompareTo(UploadExactAmount.Zero) != 0))).ToArray();
        var summary = BuildCashflowSummary(result,visibleCategories);
        summary.Add(new string?[] { "Input fingerprint", result.InputFingerprint, "Group by", result.GroupBy, "Treasury view", result.TreasuryView });
        summary.Add(new string?[] { "Selected vouchers", result.SelectedVoucherCount.ToString(CultureInfo.InvariantCulture),
            "Source lines", result.SourceLineCount.ToString(CultureInfo.InvariantCulture), "Receipt presentation", result.ReceiptPresentation });

        var reconciliationRows = new List<IReadOnlyList<string?>>
        { new string?[] { "Account", "Name", "Treasury type", "Period", "Status", "Ledger opening", "Ledger closing", "Ledger movement", "Cashflow movement", "Calculated closing", "Difference" } };
        foreach (var account in reconciliation.Accounts)
            foreach (var period in account.Periods)
                reconciliationRows.Add(new string?[] { account.AccountCode, account.AccountName, account.TreasuryType,
                    period.PeriodKey, period.Status, period.LedgerOpening, period.LedgerClosing, period.LedgerMovement,
                    period.CashflowMovement, period.CalculatedClosing, period.Difference });
        reconciliationRows.Add(new string?[] { "Overall status", reconciliation.Status, "Posting status", reconciliation.PostingStatus,
            "Evidence fingerprint", reconciliation.EvidenceFingerprint });
        reconciliationRows.Add(new string?[] { "Posting comparison", "Account", "Voucher", "Status", "Ledger lines", "Transaction lines", "Differences" });
        reconciliationRows.AddRange(reconciliation.PostingFindings.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.AccountCode, row.VoucherId, row.Status, row.LedgerLineCount.ToString(CultureInfo.InvariantCulture),
            row.TransactionLineCount.ToString(CultureInfo.InvariantCulture), string.Join(",", row.DifferenceFields) }));
        reconciliationRows.Add(new string?[] { "Presentation adjustments", "Treasury view", "Voucher", "Period", "Reason", "Label", "Category", "Receipts", "Payments" });
        reconciliationRows.AddRange(reconciliation.PresentationAdjustments.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.TreasuryView, row.VoucherId, row.PeriodKey, row.Reason, row.Label, row.Category, row.Receipts, row.Payments }));
        reconciliationRows.Add(new string?[] { "Voucher reviews", "Treasury view", "Voucher", "Period", "Reason" });
        reconciliationRows.AddRange(reconciliation.VoucherReviews.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.TreasuryView, row.VoucherId, row.PeriodKey, row.Reason }));
        reconciliationRows.Add(new string?[] { "Adjustment source postings", "Treasury view", "Source row", "Voucher", "Date", "Period", "Account", "Account name", "Category", "Debit", "Credit", "Treasury", "Category owned" });
        reconciliationRows.AddRange(reconciliation.AdjustmentEvidence.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.TreasuryView, row.SourceRowId.ToString(CultureInfo.InvariantCulture), row.VoucherId, row.Date,
            row.PeriodKey, row.AccountCode, row.AccountName, row.Category, row.Debit, row.Credit,
            row.IsTreasury.ToString(), row.CategoryOwned.ToString() }));
        reconciliationRows.Add(new string?[] { "Diagnostics" });
        reconciliationRows.AddRange(reconciliation.Diagnostics.Select(value => (IReadOnlyList<string?>)new string?[] { "", value }));

        var movement = new List<IReadOnlyList<string?>>
        { new string?[] { "Period", "Key", "Treasury type", "Source movement", "Category movement", "Internal transfer", "Mixed voucher movement", "Unallocated category", "Explained movement", "Variance", "Mixed voucher count", "Allocation status", "Status" } };
        movement.AddRange(result.TreasuryMovementProof.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.PeriodLabel, row.PeriodKey, row.TreasuryType, row.SourceMovement, row.CategoryMovement,
            row.InternalTransferMovement, row.MixedVoucherMovement, row.UnallocatedCategoryMovement,
            row.ExplainedMovement, row.Variance, row.MixedVoucherCount.ToString(CultureInfo.InvariantCulture),
            row.AllocationStatus, row.Status }));
        movement.Add(new string?[] { "Treasury split reconciliation", result.TreasuryReconciliation?.Status,
            "Maximum difference", result.TreasuryReconciliation?.MaxAbsVariance, "Mixed amount", result.TreasuryReconciliation?.MixedAmount,
            "Allocation", result.TreasuryReconciliation?.AllocationStatus });

        var transfers = new List<IReadOnlyList<string?>>
        { new string?[] { "Voucher", "Date", "Period", "Transfer type", "From account", "From name", "From type", "To account", "To name", "To type", "Amount", "Variance", "Classification", "Status", "Matched", "Bank movement", "Cash movement", "Description" } };
        transfers.AddRange(result.TransferDetails.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.VoucherId, row.Date, row.PeriodLabel, row.TransferType, row.TransferorAccountCode, row.TransferorAccountName,
            row.TransferorType, row.TransfereeAccountCode, row.TransfereeAccountName, row.TransfereeType, row.Amount,
            row.Variance, row.Classification, row.Status, row.Matched.ToString(), row.BankMovement, row.CashMovement, row.VoucherDetail }));
        var transferSummary = result.TransferReconciliation;
        transfers.Add(new string?[] { "Summary", "Candidates", transferSummary?.CandidateVoucherCount.ToString(CultureInfo.InvariantCulture),
            "Confirmed", transferSummary?.ConfirmedCount.ToString(CultureInfo.InvariantCulture), "Mixed", transferSummary?.MixedCount.ToString(CultureInfo.InvariantCulture),
            "Ambiguous", transferSummary?.AmbiguousCount.ToString(CultureInfo.InvariantCulture), "From total", transferSummary?.TransferorTotal,
            "To total", transferSummary?.TransfereeTotal, "Variance", transferSummary?.VarianceTotal, "Status", transferSummary?.Status });

        var customer = new List<IReadOnlyList<string?>>
        { new string?[] { "Voucher detail", "Voucher", "Date", "Period", "Customer account", "Customer name", "Treasury account", "Treasury name", "Treasury type", "Method", "Presentation", "Gross", "Receipts", "Reversals", "Cashflow amount", "Treasury inflow", "Charges", "Withholding tax", "Other deductions", "Deductions total", "Technical offset", "Net settlement", "Expected net", "Variance", "Status" } };
        customer.AddRange(result.CustomerPaymentDetails.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.VoucherDetail, row.VoucherId, row.Date, row.PeriodLabel, row.CustomerAccountCode, row.CustomerAccountName,
            row.BankAccountCode, row.BankAccountName, row.BankAccountType, row.Method, row.ConfiguredPresentation,
            row.CustomerGrossAmount, row.CustomerReceipts, row.CustomerReversals, row.CashflowAmount, row.BankStatementAmount,
            row.BankCharges, row.WithholdingTax, row.OtherDeductions, row.DeductionsTotal, row.TechnicalBankOffset,
            row.NetBankSettlement, row.ExpectedNetSettlement, row.Variance, row.Status }));
        customer.Add(new string?[] { "Customer totals", "Customer", "Account code", "Treasury type", "Vouchers", "Matched", "Review",
            "Gross", "Receipts", "Reversals", "Cashflow amount", "Charges", "WHT", "Other deductions", "Total deductions",
            "Technical offset", "Expected net", "Actual net", "Statement eligible", "Variance", "Status" });
        customer.AddRange(result.CustomerPaymentCustomerTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.CustomerAccountName, row.CustomerAccountCode, row.BankAccountType, row.VoucherCount.ToString(CultureInfo.InvariantCulture),
            row.MatchedVoucherCount.ToString(CultureInfo.InvariantCulture), row.ReviewVoucherCount.ToString(CultureInfo.InvariantCulture),
            row.CustomerGrossAmount, row.CustomerReceipts, row.CustomerReversals, row.CashflowAmount, row.BankCharges,
            row.WithholdingTax, row.OtherDeductions, row.DeductionsTotal, row.TechnicalBankOffset, row.ExpectedNetSettlement,
            row.NetBankSettlement, row.StatementEligibleActualSettlement, row.Variance, row.Status }));
        customer.Add(new string?[] { "Period totals", "Period", "Start", "End", "Vouchers", "Matched", "Review", "Gross", "Receipts",
            "Reversals", "Cashflow amount", "Charges", "WHT", "Other deductions", "Total deductions", "Technical offset",
            "Expected net", "Actual net", "Statement eligible", "Received from customers", "Difference", "Status" });
        customer.AddRange(result.CustomerPaymentPeriodTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.PeriodLabel, row.PeriodStart, row.PeriodEnd, row.VoucherCount.ToString(CultureInfo.InvariantCulture),
            row.MatchedVoucherCount.ToString(CultureInfo.InvariantCulture), row.ReviewVoucherCount.ToString(CultureInfo.InvariantCulture),
            row.CustomerGrossAmount, row.CustomerReceipts, row.CustomerReversals, row.CashflowAmount, row.BankCharges,
            row.WithholdingTax, row.OtherDeductions, row.DeductionsTotal, row.TechnicalBankOffset, row.ExpectedNetSettlement,
            row.NetBankSettlement, row.StatementEligibleActualSettlement, row.MainReceivedFromCustomersTotal, row.Difference, row.Status }));
        var customerReconciliation = result.CustomerPaymentReconciliation;
        customer.Add(new string?[] { "Reconciliation", "Eligible customer total", customerReconciliation?.StatementEligibleCustomerTotal,
            "Main received from customers", customerReconciliation?.MainReceivedFromCustomersTotal, "Difference", customerReconciliation?.Difference,
            "Status", customerReconciliation?.Status });
        var customerTransactions=new List<IReadOnlyList<string?>>
        { new string?[] { "Voucher detail", "Voucher", "Date", "Period", "Customer account", "Customer name", "Treasury account", "Treasury name", "Treasury type", "Method", "Presentation", "Gross", "Receipts", "Reversals", "Cashflow amount", "Treasury inflow", "Charges", "Withholding tax", "Other deductions", "Deductions total", "Technical offset", "Net settlement", "Expected net", "Variance", "Status" } };
        customerTransactions.AddRange(result.CustomerPaymentDetails.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.VoucherDetail,row.VoucherId,row.Date,row.PeriodLabel,row.CustomerAccountCode,row.CustomerAccountName,
          row.BankAccountCode,row.BankAccountName,row.BankAccountType,row.Method,row.ConfiguredPresentation,row.CustomerGrossAmount,
          row.CustomerReceipts,row.CustomerReversals,row.CashflowAmount,row.BankStatementAmount,row.BankCharges,row.WithholdingTax,
          row.OtherDeductions,row.DeductionsTotal,row.TechnicalBankOffset,row.NetBankSettlement,row.ExpectedNetSettlement,row.Variance,row.Status }));
        var customerTotals=new List<IReadOnlyList<string?>>
        { new string?[] { "Customer", "Account code", "Treasury type", "Vouchers", "Matched", "Review", "Gross", "Receipts", "Reversals", "Cashflow amount", "Charges", "WHT", "Other deductions", "Total deductions", "Technical offset", "Expected net", "Actual net", "Statement eligible", "Variance", "Status" } };
        customerTotals.AddRange(result.CustomerPaymentCustomerTotals.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.CustomerAccountName,row.CustomerAccountCode,row.BankAccountType,row.VoucherCount.ToString(CultureInfo.InvariantCulture),
          row.MatchedVoucherCount.ToString(CultureInfo.InvariantCulture),row.ReviewVoucherCount.ToString(CultureInfo.InvariantCulture),
          row.CustomerGrossAmount,row.CustomerReceipts,row.CustomerReversals,row.CashflowAmount,row.BankCharges,row.WithholdingTax,
          row.OtherDeductions,row.DeductionsTotal,row.TechnicalBankOffset,row.ExpectedNetSettlement,row.NetBankSettlement,
          row.StatementEligibleActualSettlement,row.Variance,row.Status }));

        var vendor = new List<IReadOnlyList<string?>>
        { new string?[] { "Transaction detail", "Voucher", "Date", "Period", "Account", "Name", "Category", "Payments", "Reversals", "Net payments", "Cashflow amount", "Description" } };
        vendor.AddRange(result.VendorPaymentDetails.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.VoucherId, row.Date, row.PeriodLabel, row.AccountCode, row.AccountName, row.Category,
            row.Payments, row.Reversals, row.NetPayments, row.CashflowAmount, row.VoucherDetail }));
        vendor.Add(new string?[] { "Vendor totals", "Account", "Name", "Category", "Payments", "Reversals", "Net payments", "Cashflow amount", "Voucher count" });
        vendor.AddRange(result.VendorPaymentVendorTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.AccountCode, row.AccountName, row.Category, row.Payments, row.Reversals, row.NetPayments, row.CashflowAmount,
            row.VoucherCount.ToString(CultureInfo.InvariantCulture) }));
        vendor.Add(new string?[] { "Period totals", "Period", "Account", "Name", "Category", "Payments", "Reversals", "Net payments", "Cashflow amount", "Voucher count" });
        vendor.AddRange(result.VendorPaymentPeriodTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.PeriodLabel, row.AccountCode, row.AccountName, row.Category, row.Payments, row.Reversals, row.NetPayments,
            row.CashflowAmount, row.VoucherCount.ToString(CultureInfo.InvariantCulture) }));
        var vendorTransactions=new List<IReadOnlyList<string?>>
        { new string?[] { "Voucher", "Date", "Period", "Account", "Name", "Category", "Payments", "Reversals", "Net payments", "Cashflow amount", "Description" } };
        vendorTransactions.AddRange(result.VendorPaymentDetails.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.VoucherId,row.Date,row.PeriodLabel,row.AccountCode,row.AccountName,row.Category,row.Payments,row.Reversals,
          row.NetPayments,row.CashflowAmount,row.VoucherDetail }));
        var vendorTotals=new List<IReadOnlyList<string?>>
        { new string?[] { "Account", "Name", "Category", "Payments", "Reversals", "Net payments", "Cashflow amount", "Voucher count" } };
        vendorTotals.AddRange(result.VendorPaymentVendorTotals.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.AccountCode,row.AccountName,row.Category,row.Payments,row.Reversals,row.NetPayments,row.CashflowAmount,
          row.VoucherCount.ToString(CultureInfo.InvariantCulture) }));

        var treasury = new List<IReadOnlyList<string?>>
        { new string?[] { "Category", "Period", "Bank", "Cash", "Mixed / unallocated", "Total" } };
        treasury.AddRange(result.TreasuryCategoryTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.Category, row.PeriodKey, row.BankAmount, row.CashAmount, row.MixedAmount, row.TotalAmount }));
        var review = new List<IReadOnlyList<string?>>
        { new string?[] { "Category review", "Voucher", "Date", "Period", "Account", "Name", "Debit", "Credit", "Signed amount", "Reason" } };
        review.AddRange(result.CategoriesNeedingReview.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.VoucherId, row.Date, row.PeriodLabel, row.AccountCode, row.AccountName, row.Debit, row.Credit, row.SignedAmount, row.Reason }));
        review.Add(new string?[] { "Selection allocation review", "Voucher", "Date", "Period", "Reason", "Selected receipts", "Selected payments" });
        review.AddRange(result.SelectionReviewDetails.Select(row => (IReadOnlyList<string?>)new string?[]
        { "", row.VoucherId, row.Date, row.PeriodLabel, row.Reason, row.Receipts, row.Payments }));
        var deductions=new List<IReadOnlyList<string?>>
        { new string?[] { "Date", "Month", "Period", "Category", "Account", "Name", "Signed amount", "Presentation" } };
        deductions.AddRange(result.DeductionDetails.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.Date, row.Month, row.PeriodLabel, row.Category, row.AccountCode, row.AccountName, row.Amount, row.Presentation }));
        IEnumerable<CashflowLedgerRollup> ledger = result.LedgerRollups;
        string treasuryTypeFilter = filter.TreasuryType ?? "";
        if (treasuryTypeFilter.Length == 0 && filter.TreasuryView is "bank" or "cash") treasuryTypeFilter = filter.TreasuryView;
        ledger = ledger.Where(row => Contains(row.Category, filter.Search)
            || Contains(row.AccountCode, filter.Search) || Contains(row.AccountName, filter.Search));
        ledger = ledger.Where(row => (string.IsNullOrWhiteSpace(filter.Category)
                || row.Category.Equals(filter.Category.Trim(), StringComparison.OrdinalIgnoreCase))
            && (string.IsNullOrWhiteSpace(filter.AccountHead) || row.AccountName.Equals(filter.AccountHead.Trim(), StringComparison.OrdinalIgnoreCase)
                || row.AccountCode.Equals(filter.AccountHead.Trim(), StringComparison.OrdinalIgnoreCase))
            && (string.IsNullOrWhiteSpace(treasuryTypeFilter) || row.TreasuryType.Equals(treasuryTypeFilter, StringComparison.OrdinalIgnoreCase))
            && (string.IsNullOrWhiteSpace(filter.PeriodKey)
                || row.PeriodKey.Equals(filter.PeriodKey.Trim(), StringComparison.OrdinalIgnoreCase)));

        var ledgerwise = new List<IReadOnlyList<string?>>
        { new string?[] { "Category", "Period", "Account code", "Account name", "Cash / Bank", "Amount", "Lines" } };
        ledgerwise.AddRange(result.LedgerRollups.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.Category,row.PeriodLabel,row.AccountCode,row.AccountName,row.TreasuryType,row.Amount,
            row.LineCount.ToString(CultureInfo.InvariantCulture) }));
        var ledgerDrilldown=new List<IReadOnlyList<string?>>
        { new string?[] { "Category", "Period", "Account code", "Account name", "Cash / Bank", "Amount", "Lines" } };
        ledgerDrilldown.AddRange(ledger.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.Category,row.PeriodLabel,row.AccountCode,row.AccountName,row.TreasuryType,row.Amount,
          row.LineCount.ToString(CultureInfo.InvariantCulture) }));
        var customerPeriod = new List<IReadOnlyList<string?>>
        { new string?[] { "Period","Customer","Account code","Treasury type","Vouchers","Receipts","Reversals","Gross","Deductions","Cashflow amount","Status" } };
        customerPeriod.AddRange(result.CustomerPaymentAccountPeriodTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.PeriodLabel,row.CustomerAccountName,row.CustomerAccountCode,row.BankAccountType,
            row.VoucherCount.ToString(CultureInfo.InvariantCulture),row.CustomerReceipts,row.CustomerReversals,
            row.CustomerGrossAmount,row.DeductionsTotal,row.CashflowAmount,row.Status }));
        var vendorPeriod = new List<IReadOnlyList<string?>>
        { new string?[] { "Period","Vendor","Account code","Category","Payments","Reversals","Net payments","Cashflow amount","Vouchers" } };
        vendorPeriod.AddRange(result.VendorPaymentPeriodTotals.Select(row => (IReadOnlyList<string?>)new string?[]
        { row.PeriodLabel,row.AccountName,row.AccountCode,row.Category,row.Payments,row.Reversals,
            row.NetPayments,row.CashflowAmount,row.VoucherCount.ToString(CultureInfo.InvariantCulture) }));
        var exportControls = new List<IReadOnlyList<string?>>
        { new string?[] { "Control","Value" },
          new string?[] { "Input fingerprint",result.InputFingerprint },
          new string?[] { "Period start",result.Periods.FirstOrDefault()?.Start },
          new string?[] { "Period end",result.Periods.LastOrDefault()?.End },
          new string?[] { "Grouping",result.GroupBy },new string?[] { "Treasury view",result.TreasuryView },
          new string?[] { "Summary category filter",string.IsNullOrWhiteSpace(filter.SummaryFilter)?"All categories":filter.SummaryFilter },
          new string?[] { "Hide zero summary rows",filter.HideZeroRows?"Yes":"No" },
          new string?[] { "Drill-down search",string.IsNullOrWhiteSpace(filter.Search)?"All":filter.Search },
          new string?[] { "Drill-down category",string.IsNullOrWhiteSpace(filter.Category)?"All":filter.Category },
          new string?[] { "Drill-down account",string.IsNullOrWhiteSpace(filter.AccountHead)?"All":filter.AccountHead },
          new string?[] { "Drill-down Cash / Bank",string.IsNullOrWhiteSpace(filter.TreasuryType)?"All":filter.TreasuryType },
          new string?[] { "Drill-down period",string.IsNullOrWhiteSpace(filter.PeriodKey)?"All":filter.PeriodKey },
          new string?[] { "Selected account codes",filter.SelectedAccountCodes is null?"All configured Cash/Bank accounts":string.Join(", ",filter.SelectedAccountCodes) },
          new string?[] { "Receipt presentation",result.ReceiptPresentation },
          new string?[] { "Selected vouchers",result.SelectedVoucherCount.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Source lines",result.SourceLineCount.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Transfers",result.InternalTransferCount.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Customer deductions",result.DeductionCount.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Category reviews",result.CategoryReviewCount.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Selected-account reviews",result.SelectionReviewDetails.Count.ToString(CultureInfo.InvariantCulture) },
          new string?[] { "Treasury reconciliation",result.TreasuryReconciliation?.Status },
          new string?[] { "Ledger reconciliation",reconciliation.Status },
          new string?[] { "Posting comparison",reconciliation.PostingStatus } };
        exportControls.AddRange(result.TreasuryMovementProof.Select(row=>(IReadOnlyList<string?>)new string?[]
        { "Movement proof "+row.PeriodLabel+" "+row.TreasuryType,
          "source="+row.SourceMovement+"; explained="+row.ExplainedMovement+"; variance="+row.Variance }));

        var adjustmentEvidence = new List<IReadOnlyList<string?>>
        { new string?[] { "Treasury view","Voucher","Period","Reason","Label","Category","Receipts","Payments" } };
        adjustmentEvidence.AddRange(reconciliation.PresentationAdjustments.Select(row=>(IReadOnlyList<string?>)new string?[]
        { row.TreasuryView,row.VoucherId,row.PeriodKey,row.Reason,row.Label,row.Category,row.Receipts,row.Payments }));
        adjustmentEvidence.Add(new string?[] { "Source postings","View","Row ID","Voucher","Date","Account","Category","Debit","Credit","Treasury","Owned","In period" });
        adjustmentEvidence.AddRange(reconciliation.AdjustmentEvidence.Select(row=>(IReadOnlyList<string?>)new string?[]
        { "",row.TreasuryView,row.SourceRowId.ToString(CultureInfo.InvariantCulture),row.VoucherId,row.Date,row.AccountCode,
            row.Category,row.Debit,row.Credit,row.IsTreasury.ToString(),row.CategoryOwned.ToString(),row.IsInPeriod.ToString() }));
        if(includeReconciliation&&snapshot is not null)
        {
            adjustmentEvidence.Add(new string?[]{"Complete source occurrences","Proof","Book","Row ID","Voucher","Date","Account","Account name","Debit","Credit","In period","Original row data"});
            foreach(string proof in reconciliation.PresentationAdjustments.Select(row=>row.TreasuryView).Distinct(StringComparer.Ordinal))
            {
                var membership=reconciliation.PresentationAdjustments.Where(row=>row.TreasuryView==proof)
                    .Select(row=>(row.VoucherId,row.PeriodKey)).Distinct().ToArray();
                foreach(string book in new[]{"ledger","voucher"})
                foreach(var row in FinancialReportReconciliationService.AdjustmentEvidenceRows(snapshot,result,proof,book,membership))
                    adjustmentEvidence.Add(new string?[]{"",proof,book,row.GetValueOrDefault("id")?.ToString(),
                        row.GetValueOrDefault("voucher_id")?.ToString(),row.GetValueOrDefault("date")?.ToString(),
                        row.GetValueOrDefault("account_code")?.ToString(),row.GetValueOrDefault("account_name")?.ToString(),
                        row.GetValueOrDefault("debit")?.ToString(),row.GetValueOrDefault("credit")?.ToString(),
                        row.GetValueOrDefault("in_period")?.ToString(),JsonSerializer.Serialize(row.GetValueOrDefault("row_data"))});
            }
        }

        var sheets=new List<FinancialReportWorkbookSheet>
        { new("Cashflows Summary",summary),new("Ledgerwise Summary",ledgerwise),new("Internal Transfers",transfers),
          new("Customer Deductions",deductions),new("Review Required",review),new("Cashflows Ledger Drill-down",ledgerDrilldown) };
        if(result.CustomerPaymentAccountPeriodTotals.Count>0)sheets.Add(new("Customer Period Totals",customerPeriod));
        if(result.CustomerPaymentCustomerTotals.Count>0)sheets.Add(new("Customer Totals",customerTotals));
        if(result.CustomerPaymentDetails.Count>0)sheets.Add(new("Customer Transactions",customerTransactions));
        if(result.VendorPaymentPeriodTotals.Count>0)sheets.Add(new("Vendor Period Totals",vendorPeriod));
        if(result.VendorPaymentVendorTotals.Count>0)sheets.Add(new("Vendor Totals",vendorTotals));
        if(result.VendorPaymentDetails.Count>0)sheets.Add(new("Vendor Transactions",vendorTransactions));
        sheets.Add(new("Export Controls",exportControls));
        if(result.TreasuryMovementProof.Count>0)sheets.Add(new("Treasury Movement Proof",movement));
        if(includeReconciliation)
        {sheets.Add(new("Reconciliation Proof",reconciliationRows));sheets.Add(new("Reconciliation Evidence",adjustmentEvidence));}
        return sheets;
    }

    private static List<IReadOnlyList<string?>> BuildCashflowSummary(CashflowCalculationResult result,
        IReadOnlyList<CashflowCategoryTotal> visibleCategories)
    {
        var rows=new List<IReadOnlyList<string?>>();
        string Money(UploadExactAmount value,bool payments)=>payments?value.Abs().ToFixedString():value.ToFixedString();
        string Percent(UploadExactAmount value,UploadExactAmount basis)
        {
            if(basis.CompareTo(UploadExactAmount.Zero)==0)return "0.00%";
            if(!double.TryParse(value.Abs().ToFixedString(),NumberStyles.Float,CultureInfo.InvariantCulture,out var amount)
                ||!double.TryParse(basis.Abs().ToFixedString(),NumberStyles.Float,CultureInfo.InvariantCulture,out var total)
                ||!double.IsFinite(amount)||!double.IsFinite(total)||total==0)return "Unavailable";
            return (amount/total*100).ToString("0.00",CultureInfo.InvariantCulture)+"%";
        }
        if(result.TreasuryView=="split")
        {
            var categories=result.TreasuryCategoryTotals.GroupBy(row=>row.Category,StringComparer.OrdinalIgnoreCase)
                .Where(group=>visibleCategories.Any(row=>row.Category.Equals(group.Key,StringComparison.OrdinalIgnoreCase)))
                .OrderBy(group=>group.Key,StringComparer.OrdinalIgnoreCase).ToArray();
            var headers=new List<string?>{"Category"};
            foreach(var period in result.Periods){headers.Add(period.Label+" Bank");headers.Add(period.Label+" Cash");headers.Add(period.Label+" Mixed / unresolved");headers.Add(period.Label+" Total");}
            headers.AddRange(["Bank total","Cash total","Mixed / unresolved total","Total","% of section","Local total"]);
            foreach(bool payments in new[]{false,true})
            {
                rows.Add(new string?[]{(payments?"Payments":"Receipts")+" — percentages calculated within this section"});rows.Add(headers);
                var selected=categories.Where(group=>group.Aggregate(UploadExactAmount.Zero,
                    (sum,row)=>sum+UploadExactAmount.Parse(row.TotalAmount)).CompareTo(UploadExactAmount.Zero)<0==payments).ToArray();
                var totalsByPeriod=result.Periods.ToDictionary(period=>period.Key,period=>
                    (Bank:UploadExactAmount.Zero,Cash:UploadExactAmount.Zero,Mixed:UploadExactAmount.Zero,Total:UploadExactAmount.Zero),StringComparer.Ordinal);
                foreach(var category in selected)
                {
                    var row=new List<string?>{category.Key};UploadExactAmount bankTotal=UploadExactAmount.Zero,cashTotal=UploadExactAmount.Zero,mixedTotal=UploadExactAmount.Zero,total=UploadExactAmount.Zero;
                    foreach(var period in result.Periods)
                    {
                        var entries=category.Where(item=>item.PeriodKey==period.Key).ToArray();
                        var bank=entries.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+UploadExactAmount.Parse(item.BankAmount));
                        var cash=entries.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+UploadExactAmount.Parse(item.CashAmount));
                        var mixed=entries.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+UploadExactAmount.Parse(item.MixedAmount));
                        var sum=bank+cash+mixed;row.Add(Money(bank,payments));row.Add(Money(cash,payments));row.Add(Money(mixed,payments));row.Add(Money(sum,payments));
                        var old=totalsByPeriod[period.Key];totalsByPeriod[period.Key]=(old.Bank+bank,old.Cash+cash,old.Mixed+mixed,old.Total+sum);
                        bankTotal+=bank;cashTotal+=cash;mixedTotal+=mixed;total+=sum;
                    }
                    var basis=selected.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+item.Aggregate(UploadExactAmount.Zero,
                        (part,entry)=>part+UploadExactAmount.Parse(entry.TotalAmount)).Abs());
                    row.AddRange([Money(bankTotal,payments),Money(cashTotal,payments),Money(mixedTotal,payments),Money(total,payments),Percent(total,basis),""]);
                    rows.Add(row);
                }
                var totalRow=new List<string?>{"Total "+(payments?"payments":"receipts")};
                foreach(var period in result.Periods){var value=totalsByPeriod[period.Key];totalRow.Add(Money(value.Bank,payments));totalRow.Add(Money(value.Cash,payments));totalRow.Add(Money(value.Mixed,payments));totalRow.Add(Money(value.Total,payments));}
                var bankGrand=selected.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+item.Aggregate(UploadExactAmount.Zero,(part,row)=>part+UploadExactAmount.Parse(row.BankAmount)));
                var cashGrand=selected.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+item.Aggregate(UploadExactAmount.Zero,(part,row)=>part+UploadExactAmount.Parse(row.CashAmount)));
                var mixedGrand=selected.Aggregate(UploadExactAmount.Zero,(sum,item)=>sum+item.Aggregate(UploadExactAmount.Zero,(part,row)=>part+UploadExactAmount.Parse(row.MixedAmount)));
                var totalAmount=bankGrand+cashGrand+mixedGrand;totalRow.AddRange([Money(bankGrand,payments),Money(cashGrand,payments),Money(mixedGrand,payments),Money(totalAmount,payments),"100.00%",""]);rows.Add(totalRow);
            }
        }
        else
        {
            var headers=new List<string?>{"Category"};headers.AddRange(result.Periods.Select(row=>row.Label));headers.AddRange(["Total","% of section","Local total"]);
            foreach(bool payments in new[]{false,true})
            {
                rows.Add(new string?[]{(payments?"Payments":"Receipts")+" — percentages calculated within this section"});rows.Add(headers);
                var selected=visibleCategories.Where(row=>UploadExactAmount.Parse(row.Total).CompareTo(UploadExactAmount.Zero)<0==payments).ToArray();
                var basis=selected.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+UploadExactAmount.Parse(row.Total).Abs());
                foreach(var category in selected)
                {
                    var row=new List<string?>{category.Category};
                    foreach(var period in result.Periods)row.Add(Money(UploadExactAmount.Parse(category.Periods.GetValueOrDefault(period.Key,"0")),payments));
                    var total=UploadExactAmount.Parse(category.Total);
                    row.Add(Money(total,payments));row.Add(Percent(total,basis));row.Add("");rows.Add(row);
                }
                var totalRow=new List<string?>{"Total "+(payments?"payments":"receipts")};
                foreach(var period in result.Periods)
                {var value=selected.Aggregate(UploadExactAmount.Zero,(sum,row)=>sum+UploadExactAmount.Parse(row.Periods.GetValueOrDefault(period.Key,"0")));totalRow.Add(Money(value,payments));}
                totalRow.Add(Money(basis,payments));totalRow.Add("100.00%");totalRow.Add("");rows.Add(totalRow);
            }
        }
        return rows;
    }

    private static void WriteSheet(XmlWriter writer,FinancialReportWorkbookSheet sheet,
        ReportInputOptions options,CancellationToken ct)
    {
        writer.WriteStartElement("worksheet",MainNs);
        writer.WriteStartElement("sheetViews");writer.WriteStartElement("sheetView");writer.WriteAttributeString("workbookViewId","0");
        writer.WriteStartElement("pane");writer.WriteAttributeString("ySplit","1");writer.WriteAttributeString("topLeftCell","A2");
        writer.WriteAttributeString("activePane","bottomLeft");writer.WriteAttributeString("state","frozen");writer.WriteEndElement();
        writer.WriteEndElement();writer.WriteEndElement();
        writer.WriteStartElement("sheetData");
        long cells=0;int rowNumber=0;
        var numericColumns=NumericColumns(sheet);
        foreach(var row in sheet.Rows)
        {
            ct.ThrowIfCancellationRequested();if(++rowNumber>options.MaximumWorkbookRows)throw new InvalidDataException("Financial report workbook row capacity exceeded.");
            writer.WriteStartElement("row");writer.WriteAttributeString("r",rowNumber.ToString());
            int style=RowStyle(row,rowNumber);
            for(int column=0;column<row.Count;column++)
            {
                if(++cells>(long)options.MaximumWorkbookRows*100)throw new InvalidDataException("Financial report workbook cell capacity exceeded.");
                writer.WriteStartElement("c");writer.WriteAttributeString("r",Column(column+1)+rowNumber);
                if(style>0)writer.WriteAttributeString("s",style.ToString(CultureInfo.InvariantCulture));
                string? value=row[column];
                if(value is not null&&numericColumns.Contains(column)&&IsSafeExcelNumber(value,out var numeric))
                {
                    writer.WriteStartElement("v","http://schemas.openxmlformats.org/spreadsheetml/2006/main");
                    writer.WriteString(numeric);writer.WriteEndElement();writer.WriteEndElement();
                }
                else
                {
                    writer.WriteAttributeString("t","inlineStr");
                    writer.WriteStartElement("is");writer.WriteStartElement("t","http://schemas.openxmlformats.org/spreadsheetml/2006/main");
                    writer.WriteAttributeString("xml","space","http://www.w3.org/XML/1998/namespace","preserve");
                    writer.WriteString(value??"");writer.WriteEndElement();writer.WriteEndElement();writer.WriteEndElement();
                }
            }
            writer.WriteEndElement();
        }
        writer.WriteEndElement();writer.WriteEndElement();
    }

    private static HashSet<int> NumericColumns(FinancialReportWorkbookSheet sheet)
    {
        var result=new HashSet<int>();if(sheet.Rows.Count==0)return result;
        for(int index=0;index<sheet.Rows[0].Count;index++)
        {
            string header=sheet.Rows[0][index]?.Trim()??"";
            string normalized=header.ToLowerInvariant();
            if(sheet.Name=="Cashflows Summary"&&index>0&&!normalized.Contains("percentage")&&!normalized.Contains("local total"))
            {result.Add(index);continue;}
            if(new[]{"amount","debit","credit","balance","movement","difference","variance","receipt","payment",
                "gross","net","total","opening","closing","count","lines","vouchers","adjustment","cashflow","ledger","percent"}
                .Any(term=>normalized.Contains(term,StringComparison.Ordinal)))result.Add(index);
        }
        return result;
    }

    private static bool IsSafeExcelNumber(string value,out string normalized)
    {
        normalized="";string text=value.Trim();
        if(text.Length is 0 or >128||!decimal.TryParse(text,NumberStyles.AllowLeadingSign|NumberStyles.AllowDecimalPoint,
            CultureInfo.InvariantCulture,out _))return false;
        int digits=text.Count(char.IsAsciiDigit);if(digits>15)return false;
        normalized=text;return true;
    }

    private static int RowStyle(IReadOnlyList<string?> row, int rowNumber)
    {
        if (rowNumber == 1) return 1;
        string first = row.FirstOrDefault()?.Trim() ?? "";
        if (first.StartsWith("Total", StringComparison.OrdinalIgnoreCase)
            || first.StartsWith("Overall", StringComparison.OrdinalIgnoreCase)
            || first.Equals("Summary", StringComparison.OrdinalIgnoreCase)
            || first.Equals("Reconciliation", StringComparison.OrdinalIgnoreCase)
            || first.Equals("Ledger details", StringComparison.OrdinalIgnoreCase)) return 2;
        if (row.Any(value => string.Equals(value, "Variance", StringComparison.OrdinalIgnoreCase)
            || string.Equals(value, "Review required", StringComparison.OrdinalIgnoreCase)
            || string.Equals(value, "Unavailable", StringComparison.OrdinalIgnoreCase))) return 4;
        if (row.Any(value => string.Equals(value, "Matched", StringComparison.OrdinalIgnoreCase))) return 3;
        return 0;
    }

    private static void WriteStyles(XmlWriter writer)
    {
        writer.WriteStartElement("styleSheet",MainNs);
        writer.WriteStartElement("fonts");writer.WriteAttributeString("count","3");
        Font(writer,false,"FF1A1A2E");Font(writer,true,"FFFFFFFF");Font(writer,true,"FF18344B");writer.WriteEndElement();
        writer.WriteStartElement("fills");writer.WriteAttributeString("count","6");
        Fill(writer,"none",null);Fill(writer,"gray125",null);Fill(writer,"solid","FF18344B");
        Fill(writer,"solid","FFDCEAF4");Fill(writer,"solid","FFE2F0D9");Fill(writer,"solid","FFFCE4D6");writer.WriteEndElement();
        writer.WriteStartElement("borders");writer.WriteAttributeString("count","1");writer.WriteStartElement("border");
        writer.WriteElementString("left",MainNs,"");writer.WriteElementString("right",MainNs,"");
        writer.WriteElementString("top",MainNs,"");writer.WriteElementString("bottom",MainNs,"");
        writer.WriteElementString("diagonal",MainNs,"");writer.WriteEndElement();writer.WriteEndElement();
        writer.WriteStartElement("cellStyleXfs");writer.WriteAttributeString("count","1");writer.WriteStartElement("xf");
        writer.WriteAttributeString("numFmtId","0");writer.WriteAttributeString("fontId","0");writer.WriteAttributeString("fillId","0");
        writer.WriteAttributeString("borderId","0");writer.WriteEndElement();writer.WriteEndElement();
        writer.WriteStartElement("cellXfs");writer.WriteAttributeString("count","5");
        Style(writer,0,0);Style(writer,1,2);Style(writer,2,3);Style(writer,2,4);Style(writer,2,5);
        writer.WriteEndElement();writer.WriteStartElement("cellStyles");writer.WriteAttributeString("count","1");
        writer.WriteStartElement("cellStyle");writer.WriteAttributeString("name","Normal");writer.WriteAttributeString("xfId","0");
        writer.WriteAttributeString("builtinId","0");writer.WriteEndElement();writer.WriteEndElement();writer.WriteEndElement();
    }

    private static void Font(XmlWriter writer,bool bold,string color)
    {
        writer.WriteStartElement("font");if(bold)writer.WriteElementString("b",MainNs,"");
        writer.WriteStartElement("color");writer.WriteAttributeString("rgb",color);writer.WriteEndElement();
        writer.WriteStartElement("sz");writer.WriteAttributeString("val","10");writer.WriteEndElement();
        writer.WriteStartElement("name");writer.WriteAttributeString("val","Arial");writer.WriteEndElement();writer.WriteEndElement();
    }
    private static void Fill(XmlWriter writer,string pattern,string? color)
    {
        writer.WriteStartElement("fill");writer.WriteStartElement("patternFill");writer.WriteAttributeString("patternType",pattern);
        if(color is not null){writer.WriteStartElement("fgColor");writer.WriteAttributeString("rgb",color);writer.WriteEndElement();writer.WriteStartElement("bgColor");writer.WriteAttributeString("indexed","64");writer.WriteEndElement();}
        writer.WriteEndElement();writer.WriteEndElement();
    }
    private static void Style(XmlWriter writer,int font,int fill)
    {writer.WriteStartElement("xf");writer.WriteAttributeString("numFmtId","0");writer.WriteAttributeString("fontId",font.ToString(CultureInfo.InvariantCulture));
     writer.WriteAttributeString("fillId",fill.ToString(CultureInfo.InvariantCulture));writer.WriteAttributeString("borderId","0");
     writer.WriteAttributeString("xfId","0");writer.WriteAttributeString("applyFont","1");writer.WriteAttributeString("applyFill","1");writer.WriteEndElement();}

    private static void WriteEntry(ZipArchive zip,string name,Action<XmlWriter> body)
    {
        var entry=zip.CreateEntry(name,CompressionLevel.Fastest);entry.LastWriteTime=new DateTimeOffset(2000,1,1,0,0,0,TimeSpan.Zero);
        using var stream=entry.Open();using var writer=XmlWriter.Create(stream,new XmlWriterSettings
        {Encoding=new UTF8Encoding(false),CheckCharacters=true,CloseOutput=false});body(writer);
    }
    private static bool ValidSheetName(string name)=>name.Length is >0 and <=31
        &&name.IndexOfAny(['[',']','*','?','/','\\',':'])<0;
    private static string Column(int number){var value="";while(number>0){number--;value=(char)('A'+number%26)+value;number/=26;}return value;}
    private sealed class CappedStream(long maximum):MemoryStream
    {
        private void Check(int count){if(Length+count>maximum)throw new InvalidDataException("Financial report workbook byte capacity exceeded.");}
        public override void Write(byte[] buffer,int offset,int count){Check(count);base.Write(buffer,offset,count);}
        public override void Write(ReadOnlySpan<byte> buffer){Check(buffer.Length);base.Write(buffer);}
        public override void WriteByte(byte value){Check(1);base.WriteByte(value);}
    }
}
