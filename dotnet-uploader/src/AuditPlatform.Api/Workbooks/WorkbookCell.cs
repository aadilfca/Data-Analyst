namespace AuditPlatform.Api.Workbooks;

public enum WorkbookCellKind { Blank,Text,Number,Boolean,DateTime,Time,Duration,Error }
public enum WorkbookFormulaState { Unknown,None,Cached,MissingResult }

// NumberToken is reader metadata, not a replacement storage/API type. XLSX
// retains its numeric lexeme; binary XLS retains a round-trip IEEE value.
public sealed record WorkbookCell(int Row,int Column,WorkbookCellKind Kind,string? Text=null,
    string? NumberToken=null,bool? Boolean=null,DateTime? DateTime=null,TimeSpan? Time=null,
    string? Error=null,string? NumberFormat=null,WorkbookFormulaState FormulaState=WorkbookFormulaState.Unknown,
    string? Formula=null,string? SourceToken=null);
public sealed record WorkbookRow(string Sheet,int Number,IReadOnlyList<WorkbookCell> Cells);
public sealed record WorkbookSheet(string Name,IReadOnlyList<WorkbookCell> Header);
public sealed record WorkbookReadLimits(long MaximumFileBytes=67108864,long MaximumExpandedBytes=268435456,
    int MaximumSheets=100,int MaximumColumns=1024,int MaximumRows=1048576,int MaximumCellCharacters=65536);
public sealed class WorkbookReadException(string message) : Exception(message);
