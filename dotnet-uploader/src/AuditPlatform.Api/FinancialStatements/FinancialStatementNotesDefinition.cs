namespace AuditPlatform.Api.FinancialStatements;

internal sealed record FinancialStatementNotesResult(string StatementSection,
    IReadOnlyList<FinancialStatementNoteNode> Roots, IReadOnlyList<FinancialStatementNoteCoverage> Coverage);

internal sealed record FinancialStatementNoteNode(int Id, string Label, int? SourceStatementCategoryId,
    int? ParentCategoryId, int SortOrder, bool IsActive, string CurrentAmount, string? ComparativeAmount,
    string DirectCurrentAmount, string? DirectComparativeAmount, int DirectAccountCount, int AccountCount,
    string? CurrentSystemAmount, string? ComparativeSystemAmount,
    IReadOnlyList<FinancialStatementNoteNode> Children, bool IsUnallocatedSummary = false);

internal sealed record FinancialStatementNoteCoverage(int StatementCategoryId, string Label, int? NoteRootId,
    int AccountCount, int AssignedAccountCount, int UnassignedAccountCount,
    string? CurrentDifference, string? ComparativeDifference);
