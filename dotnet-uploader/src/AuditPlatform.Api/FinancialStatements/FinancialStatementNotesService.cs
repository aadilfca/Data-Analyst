using AuditPlatform.Api.Reports;
using AuditPlatform.Api.TrialBalance;
using AuditPlatform.Api.Uploads;

namespace AuditPlatform.Api.FinancialStatements;

/// <summary>Calculates ACC-linked Notes from the same immutable statement snapshot.</summary>
internal sealed class FinancialStatementNotesService
{
    private sealed class Totals
    {
        public UploadExactAmount Current = UploadExactAmount.Zero;
        public UploadExactAmount Comparative = UploadExactAmount.Zero;
        public int AccountCount;
        public UploadExactAmount? CurrentSystem;
        public UploadExactAmount? ComparativeSystem;
    }

    private sealed record MutableNode(ReportCategory Source, int? InheritedSourceId,
        string Label, List<MutableNode> Children, Totals Totals);

    public FinancialStatementNotesResult Calculate(ReportInputSnapshot snapshot,
        FinancialStatementResult statement, FinancialStatementSection section,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ArgumentNullException.ThrowIfNull(statement);
        if (snapshot.Scope.Kind != ReportInputKind.FinancialStatements
            || !snapshot.Categories.Complete || !snapshot.AccountAssignments.Complete
            || !snapshot.TrialBalanceAccounts.Complete || !snapshot.TrialBalanceUploads.Complete)
            throw new FinancialStatementInputException("Complete statement inputs are required to calculate Notes.");

        string sectionKey = section == FinancialStatementSection.BalanceSheet ? "balance_sheet" : "income_statement";
        var statementCategories = statement.Statement.Categories
            .Where(row => row.RowType == "category" && row.Section == sectionKey)
            .ToDictionary(row => row.Id);
        var comparativeCategories = statement.Comparative?.Categories
            .Where(row => row.RowType == "category")
            .ToDictionary(row => row.Id) ?? [];
        var noteRows = snapshot.Categories.Items.Where(row => row.CategoryType == "note")
            .ToDictionary(row => row.Id);
        var children = noteRows.Values.Where(row => row.ParentCategoryId is not null
                && noteRows.ContainsKey(row.ParentCategoryId.Value))
            .GroupBy(row => row.ParentCategoryId!.Value)
            .ToDictionary(group => group.Key, group => group.OrderBy(row => row.SortOrder).ThenBy(row => row.Id).ToArray());
        var roots = noteRows.Values.Where(row => row.ParentCategoryId is null && row.SourceStatementCategoryId is not null)
            .OrderBy(row => statementCategories.GetValueOrDefault(row.SourceStatementCategoryId!.Value)?.SortOrder ?? 0)
            .ThenBy(row => row.SortOrder).ThenBy(row => row.Id).ToArray();
        if (section == FinancialStatementSection.IncomeStatement)
        {
            var linkedCategoryIds = roots.Where(row => row.SourceStatementCategoryId is not null)
                .Select(row => row.SourceStatementCategoryId!.Value).ToHashSet();
            var missingRoles = statementCategories.Values
                .Where(row => (row.AccountCount > 0 || linkedCategoryIds.Contains(row.Id))
                    && row.EffectiveIncomeStatementRole is not ("income" or "expense"))
                .OrderBy(row => row.Label, StringComparer.Ordinal)
                .Select(row => row.Label).ToArray();
            if (missingRoles.Length > 0)
            {
                string labels = string.Join(", ", missingRoles.Take(10));
                if (missingRoles.Length > 10) labels += $" and {missingRoles.Length - 10} more";
                throw new FinancialStatementInputException(
                    "Set an Income/Expense role in Financial Statements for: " + labels);
            }
        }
        var totals = noteRows.Keys.ToDictionary(id => id, _ => new Totals());
        var assignedByCategory = statementCategories.Keys.ToDictionary(id => id, _ => 0);
        var assignments = snapshot.AccountAssignments.Items.ToDictionary(row => row.AccountCode.Trim(), StringComparer.Ordinal);
        if (assignments.Count != snapshot.AccountAssignments.Items.Count)
            throw new FinancialStatementInputException("Duplicate Trial Balance account assignments require review.");

        var hierarchy = TrialBalanceValidation.Hierarchy(
            snapshot.TrialBalanceAccounts.Items.Select(row => row.AccountCode), cancellationToken);
        foreach (var account in snapshot.TrialBalanceAccounts.Items)
        {
            cancellationToken.ThrowIfCancellationRequested();
            string code = account.AccountCode.Trim();
            string classification = account.ManuallyOverridden ? account.Classification
                : hierarchy.GetValueOrDefault(code).Classification ?? "review";
            if (!string.Equals(classification, "leaf", StringComparison.OrdinalIgnoreCase)) continue;
            if (!assignments.TryGetValue(account.AccountCode.Trim(), out var assignment)
                || assignment.FinancialStatementCategoryId is not { } categoryId
                || assignment.NoteCategoryId is not { } noteId
                || !statementCategories.TryGetValue(categoryId, out var category)
                || !noteRows.TryGetValue(noteId, out var note)
                || Root(note, noteRows) is not { } root
                || root.SourceStatementCategoryId != categoryId)
                continue;

            UploadExactAmount sign;
            if (section == FinancialStatementSection.IncomeStatement)
            {
                string? role = category.EffectiveIncomeStatementRole;
                if (role is not ("income" or "expense")) continue;
                sign = role == "income" ? UploadExactAmount.Parse("-1") : UploadExactAmount.Parse("1");
            }
            else
            {
                string? role = category.EffectivePresentationRole;
                if (role is not ("asset" or "liability" or "equity")) continue;
                sign = role == "asset" ? UploadExactAmount.Parse("1") : UploadExactAmount.Parse("-1");
            }
            var value = totals[noteId];
            var currentNet = Net(account.ClosingDebit, account.ClosingCredit);
            value.Current += sign.IsZero || sign.CompareTo(UploadExactAmount.Zero) > 0 ? currentNet : UploadExactAmount.Zero - currentNet;
            if (section == FinancialStatementSection.BalanceSheet)
            {
                var openingNet = Net(account.OpeningDebit, account.OpeningCredit);
                value.Comparative += sign.IsZero || sign.CompareTo(UploadExactAmount.Zero) > 0 ? openingNet : UploadExactAmount.Zero - openingNet;
            }
            value.AccountCount++;
            assignedByCategory[categoryId]++;
        }

        if (section == FinancialStatementSection.BalanceSheet)
        {
            foreach (var category in statementCategories.Values)
            {
                var root = roots.FirstOrDefault(row => row.SourceStatementCategoryId == category.Id);
                if (root is null) continue;
                var current = totals[root.Id];
                if (category.LinkedProfit is not null)
                {
                    current.Current += UploadExactAmount.Parse(category.LinkedProfit);
                    current.CurrentSystem = UploadExactAmount.Parse(category.LinkedProfit);
                }
                if (comparativeCategories.TryGetValue(category.Id, out var comparative) && comparative.LinkedProfit is not null)
                {
                    current.Comparative += UploadExactAmount.Parse(comparative.LinkedProfit);
                    current.ComparativeSystem = UploadExactAmount.Parse(comparative.LinkedProfit);
                }
            }
        }

        var mutableRoots = roots.Where(row => row.SourceStatementCategoryId is { } id && statementCategories.ContainsKey(id))
            .Select(row => BuildNode(row, row.SourceStatementCategoryId, noteRows, children, totals, statementCategories))
            .ToList();
        var rootsByCategory = mutableRoots.ToDictionary(row => row.Source.SourceStatementCategoryId!.Value);
        var coverage = new List<FinancialStatementNoteCoverage>();
        foreach (var category in statementCategories.Values.OrderBy(row => row.SortOrder).ThenBy(row => row.Id))
        {
            var currentRoot = rootsByCategory.GetValueOrDefault(category.Id);
            var noteCurrent = currentRoot is null ? UploadExactAmount.Zero : TreeAmount(currentRoot, opening: false);
            var noteOpening = currentRoot is null ? UploadExactAmount.Zero : TreeAmount(currentRoot, opening: true);
            UploadExactAmount? statementCurrent = category.Amount is null ? null : UploadExactAmount.Parse(category.Amount);
            if (statementCurrent is not null && section == FinancialStatementSection.IncomeStatement
                && category.EffectiveIncomeStatementRole == "expense") statementCurrent = UploadExactAmount.Zero - statementCurrent.Value;
            UploadExactAmount? statementOpening = comparativeCategories.TryGetValue(category.Id, out var comparative)
                && comparative.Amount is not null ? UploadExactAmount.Parse(comparative.Amount) : null;
            int assignedCount = assignedByCategory.GetValueOrDefault(category.Id);
            int unassigned = Math.Max(0, category.AccountCount - assignedCount);
            UploadExactAmount? currentDifference = statementCurrent is null ? null : statementCurrent.Value - noteCurrent;
            UploadExactAmount? comparativeDifference = section == FinancialStatementSection.BalanceSheet && statementOpening is not null
                ? statementOpening.Value - noteOpening : null;
            coverage.Add(new(category.Id, category.Label, currentRoot?.Source.Id, category.AccountCount,
                assignedCount, unassigned, currentDifference?.ToFixedString(), comparativeDifference?.ToFixedString()));

            bool hasCurrentGap = currentDifference is { } c && c.Abs().CompareTo(UploadExactAmount.Parse("0.01")) > 0;
            bool hasOpeningGap = comparativeDifference is { } o && o.Abs().CompareTo(UploadExactAmount.Parse("0.01")) > 0;
            if (unassigned == 0 && !hasCurrentGap && !hasOpeningGap) continue;
            var pendingTotals = new Totals
            {
                Current = currentDifference ?? UploadExactAmount.Zero,
                Comparative = comparativeDifference ?? UploadExactAmount.Zero,
                AccountCount = unassigned
            };
            int pendingId = -(2 * category.Id + 1);
            int? parentId = currentRoot?.Source.Id ?? -(2 * category.Id);
            var pendingSource = new ReportCategory(pendingId, snapshot.Scope.ClientId, "note", "unallocated",
                unassigned > 0 ? "Unassigned" : "Unallocated / awaiting note detail", null, null, null,
                "category", parentId, category.Id, int.MaxValue, true);
            var pending = new MutableNode(pendingSource, category.Id, pendingSource.Label, [], pendingTotals);
            if (currentRoot is not null) currentRoot.Children.Add(pending);
            else
            {
                var syntheticSource = pendingSource with { Id = -2 * category.Id, Label = category.Label, ParentCategoryId = null };
                var synthetic = new MutableNode(syntheticSource, category.Id, category.Label, [pending], new Totals
                { });
                mutableRoots.Add(synthetic);
                rootsByCategory[category.Id] = synthetic;
            }
        }

        var outputRoots = mutableRoots.OrderBy(row => statementCategories.GetValueOrDefault(row.Source.SourceStatementCategoryId ?? 0)?.SortOrder ?? int.MaxValue)
            .ThenBy(row => row.Source.SortOrder).ThenBy(row => row.Source.Id)
            .Select(row => ToOutput(row, section == FinancialStatementSection.BalanceSheet)).ToArray();
        return new(sectionKey, outputRoots, coverage);
    }

    private static ReportCategory? Root(ReportCategory note, IReadOnlyDictionary<int, ReportCategory> all)
    {
        var seen = new HashSet<int>();
        while (note.ParentCategoryId is { } parent)
        {
            if (!seen.Add(note.Id) || !all.TryGetValue(parent, out note!)) return null;
        }
        return note;
    }

    private static MutableNode BuildNode(ReportCategory source, int? inheritedSourceId,
        IReadOnlyDictionary<int, ReportCategory> notes, IReadOnlyDictionary<int, ReportCategory[]> children,
        IReadOnlyDictionary<int, Totals> totals, IReadOnlyDictionary<int, FinancialStatementCategory> categories)
    {
        int? sourceId = source.SourceStatementCategoryId ?? inheritedSourceId;
        string label = source.SourceStatementCategoryId is { } id && categories.TryGetValue(id, out var category)
            ? category.Label : source.Label;
        var childNodes = children.GetValueOrDefault(source.Id, [])
            .Select(child => BuildNode(child, sourceId, notes, children, totals, categories)).ToList();
        return new(source, sourceId, label, childNodes, totals[source.Id]);
    }

    private static FinancialStatementNoteNode ToOutput(MutableNode node, bool hasComparative)
    {
        var current = node.Totals.Current;
        var comparative = node.Totals.Comparative;
        int count = node.Totals.AccountCount;
        var children = node.Children.Select(child => ToOutput(child, hasComparative)).ToArray();
        foreach (var child in children)
        {
            current += UploadExactAmount.Parse(child.CurrentAmount);
            if (child.ComparativeAmount is not null) comparative += UploadExactAmount.Parse(child.ComparativeAmount);
            count += child.AccountCount;
        }
        return new(node.Source.Id, node.Label, node.InheritedSourceId, node.Source.ParentCategoryId, node.Source.SortOrder,
            node.Source.IsActive, current.ToFixedString(), hasComparative ? comparative.ToFixedString() : null,
            node.Totals.Current.ToFixedString(), hasComparative ? node.Totals.Comparative.ToFixedString() : null,
            node.Totals.AccountCount, count, node.Totals.CurrentSystem?.ToFixedString(),
            node.Totals.ComparativeSystem?.ToFixedString(), children, node.Source.Id < 0);
    }

    private static UploadExactAmount TreeAmount(MutableNode node, bool opening)
    {
        var amount = opening ? node.Totals.Comparative : node.Totals.Current;
        foreach (var child in node.Children) amount += TreeAmount(child, opening);
        return amount;
    }

    private static UploadExactAmount Net(ReportAmountValue? debit, ReportAmountValue? credit) =>
        (debit?.Amount ?? UploadExactAmount.Zero) - (credit?.Amount ?? UploadExactAmount.Zero);
}
