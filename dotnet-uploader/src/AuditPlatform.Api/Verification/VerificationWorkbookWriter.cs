using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Xml;
using AuditPlatform.Api.Reports;

namespace AuditPlatform.Api.Verification;

/// <summary>Writes a bounded Verification worksheet with literal text and exact safe numeric cells.</summary>
internal static class VerificationWorkbookWriter
{
    private const string MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    private const string RelNs = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    private const string PackageNs = "http://schemas.openxmlformats.org/package/2006/relationships";
    private static readonly string[] Headers = ["Sr No.", "Date", "Account Code", "Account Type", "Account Name",
        "Amount", "Procedures", "Observations", "Status"];
    private static readonly string[] CashbookHeaders = ["Voucher ID", "Date", "Description", "Account Code",
        "Account Name", "Amount", "Vendor", "Unit", "Qty", "Rate", "Calculated Amount", "Purchase Demand",
        "Purchase Order", "Gate Pass", "Store Receiving", "Vendor Bill", "Observations", "Comments",
        "Validation Status", "Verification Status", "Verified By", "Verified At"];

    public static byte[] Write(IReadOnlyList<VerificationWorkbookExportRow> rows, ReportInputOptions options,
        CancellationToken ct)
    {
        if (!options.Valid() || rows.Count == 0 || rows.Count + 1 > options.MaximumWorkbookRows)
            throw new InvalidDataException("Verification workbook exceeds its configured row limit.");
        using var output = new CappedStream(options.MaximumWorkbookBytes);
        using (var archive = new ZipArchive(output, ZipArchiveMode.Create, true, Encoding.UTF8))
        {
            WriteEntry(archive, "[Content_Types].xml", writer =>
            {
                writer.WriteStartElement("Types", "http://schemas.openxmlformats.org/package/2006/content-types");
                writer.WriteStartElement("Default"); writer.WriteAttributeString("Extension", "rels");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-package.relationships+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Default"); writer.WriteAttributeString("Extension", "xml");
                writer.WriteAttributeString("ContentType", "application/xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/workbook.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/styles.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/worksheets/sheet1.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"); writer.WriteEndElement();
                writer.WriteEndElement();
            });
            WriteEntry(archive, "_rels/.rels", writer =>
            {
                writer.WriteStartElement("Relationships", PackageNs); writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id", "rId1"); writer.WriteAttributeString("Type", RelNs + "/officeDocument");
                writer.WriteAttributeString("Target", "xl/workbook.xml"); writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/workbook.xml", writer =>
            {
                writer.WriteStartElement("workbook", MainNs); writer.WriteAttributeString("xmlns", "r", null, RelNs);
                writer.WriteStartElement("sheets"); writer.WriteStartElement("sheet");
                writer.WriteAttributeString("name", "Verification"); writer.WriteAttributeString("sheetId", "1");
                writer.WriteAttributeString("r", "id", RelNs, "rId1"); writer.WriteEndElement();
                writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/_rels/workbook.xml.rels", writer =>
            {
                writer.WriteStartElement("Relationships", PackageNs); writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id", "rId1"); writer.WriteAttributeString("Type", RelNs + "/worksheet");
                writer.WriteAttributeString("Target", "worksheets/sheet1.xml"); writer.WriteEndElement();
                writer.WriteStartElement("Relationship"); writer.WriteAttributeString("Id", "rId2");
                writer.WriteAttributeString("Type", RelNs + "/styles"); writer.WriteAttributeString("Target", "styles.xml");
                writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/styles.xml", WriteStyles);
            WriteEntry(archive, "xl/worksheets/sheet1.xml", writer => WriteSheet(writer, rows, options, ct));
        }
        return output.ToArray();
    }

    public static byte[] WriteAuditedCashbook(IReadOnlyList<AuditedCashbookExportRow> rows,
        ReportInputOptions options, CancellationToken ct)
    {
        if (!options.Valid() || rows.Count == 0 || rows.Count + 1 > options.MaximumWorkbookRows)
            throw new InvalidDataException("Audited Cashbook workbook exceeds its configured row limit.");
        using var output = new CappedStream(options.MaximumWorkbookBytes);
        using (var archive = new ZipArchive(output, ZipArchiveMode.Create, true, Encoding.UTF8))
        {
            WriteEntry(archive, "[Content_Types].xml", writer =>
            {
                writer.WriteStartElement("Types", "http://schemas.openxmlformats.org/package/2006/content-types");
                writer.WriteStartElement("Default"); writer.WriteAttributeString("Extension", "rels");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-package.relationships+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Default"); writer.WriteAttributeString("Extension", "xml");
                writer.WriteAttributeString("ContentType", "application/xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/workbook.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/styles.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"); writer.WriteEndElement();
                writer.WriteStartElement("Override"); writer.WriteAttributeString("PartName", "/xl/worksheets/sheet1.xml");
                writer.WriteAttributeString("ContentType", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"); writer.WriteEndElement();
                writer.WriteEndElement();
            });
            WriteEntry(archive, "_rels/.rels", writer =>
            {
                writer.WriteStartElement("Relationships", PackageNs); writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id", "rId1"); writer.WriteAttributeString("Type", RelNs + "/officeDocument");
                writer.WriteAttributeString("Target", "xl/workbook.xml"); writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/workbook.xml", writer =>
            {
                writer.WriteStartElement("workbook", MainNs); writer.WriteAttributeString("xmlns", "r", null, RelNs);
                writer.WriteStartElement("sheets"); writer.WriteStartElement("sheet");
                writer.WriteAttributeString("name", "Audited Cashbook"); writer.WriteAttributeString("sheetId", "1");
                writer.WriteAttributeString("r", "id", RelNs, "rId1"); writer.WriteEndElement();
                writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/_rels/workbook.xml.rels", writer =>
            {
                writer.WriteStartElement("Relationships", PackageNs); writer.WriteStartElement("Relationship");
                writer.WriteAttributeString("Id", "rId1"); writer.WriteAttributeString("Type", RelNs + "/worksheet");
                writer.WriteAttributeString("Target", "worksheets/sheet1.xml"); writer.WriteEndElement();
                writer.WriteStartElement("Relationship"); writer.WriteAttributeString("Id", "rId2");
                writer.WriteAttributeString("Type", RelNs + "/styles"); writer.WriteAttributeString("Target", "styles.xml");
                writer.WriteEndElement(); writer.WriteEndElement();
            });
            WriteEntry(archive, "xl/styles.xml", WriteStyles);
            WriteEntry(archive, "xl/worksheets/sheet1.xml", writer => WriteCashbookSheet(writer, rows, options, ct));
        }
        return output.ToArray();
    }

    private static void WriteCashbookSheet(XmlWriter writer, IReadOnlyList<AuditedCashbookExportRow> rows,
        ReportInputOptions options, CancellationToken ct)
    {
        writer.WriteStartElement("worksheet", MainNs);
        writer.WriteStartElement("sheetViews"); writer.WriteStartElement("sheetView");
        writer.WriteAttributeString("workbookViewId", "0"); writer.WriteStartElement("pane");
        writer.WriteAttributeString("ySplit", "1"); writer.WriteAttributeString("topLeftCell", "A2");
        writer.WriteAttributeString("activePane", "bottomLeft"); writer.WriteAttributeString("state", "frozen");
        writer.WriteEndElement(); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("cols");
        for (int column = 0; column < CashbookHeaders.Length; column++)
        {
            int longest = CashbookHeaders[column].Length;
            foreach (var row in rows) longest = Math.Max(longest, CashbookText(row, column).Length);
            writer.WriteStartElement("col"); writer.WriteAttributeString("min", (column + 1).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("max", (column + 1).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("width", Math.Clamp(longest + 2, 8, 50).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("customWidth", "1"); writer.WriteEndElement();
        }
        writer.WriteEndElement(); writer.WriteStartElement("sheetData");
        WriteCashbookRow(writer, 1, CashbookHeaders, null);
        int rowNumber = 1;
        foreach (var row in rows)
        {
            ct.ThrowIfCancellationRequested();
            if (++rowNumber > options.MaximumWorkbookRows) throw new InvalidDataException("Audited Cashbook workbook row limit exceeded.");
            WriteCashbookRow(writer, rowNumber, row.Cells, row.Amount);
        }
        writer.WriteEndElement(); writer.WriteStartElement("autoFilter", MainNs);
        writer.WriteAttributeString("ref", $"A1:V{rowNumber.ToString(CultureInfo.InvariantCulture)}");
        writer.WriteEndElement(); writer.WriteEndElement();
    }

    private static void WriteCashbookRow(XmlWriter writer, int rowNumber, IReadOnlyList<string?> values, decimal? amount)
    {
        writer.WriteStartElement("row"); writer.WriteAttributeString("r", rowNumber.ToString(CultureInfo.InvariantCulture));
        for (int index = 0; index < CashbookHeaders.Length; index++)
        {
            string? value = index < values.Count ? values[index] : "";
            writer.WriteStartElement("c"); writer.WriteAttributeString("r", Column(index + 1) + rowNumber.ToString(CultureInfo.InvariantCulture));
            if (rowNumber == 1) writer.WriteAttributeString("s", "1");
            if (rowNumber > 1 && index == 5 && amount is decimal numeric
                && SafeNumber(numeric.ToString(CultureInfo.InvariantCulture), out string safe))
            { writer.WriteStartElement("v", MainNs); writer.WriteString(safe); writer.WriteEndElement(); }
            else
            {
                writer.WriteAttributeString("t", "inlineStr"); writer.WriteStartElement("is");
                writer.WriteStartElement("t", MainNs); writer.WriteAttributeString("xml", "space", "http://www.w3.org/XML/1998/namespace", "preserve");
                writer.WriteString(value ?? ""); writer.WriteEndElement(); writer.WriteEndElement();
            }
            writer.WriteEndElement();
        }
        writer.WriteEndElement();
    }

    private static string CashbookText(AuditedCashbookExportRow row, int index) =>
        index == 5 && row.Amount.HasValue ? row.Amount.Value.ToString(CultureInfo.InvariantCulture)
            : index < row.Cells.Count ? row.Cells[index] ?? "" : "";

    private static void WriteSheet(XmlWriter writer, IReadOnlyList<VerificationWorkbookExportRow> rows,
        ReportInputOptions options, CancellationToken ct)
    {
        writer.WriteStartElement("worksheet", MainNs);
        writer.WriteStartElement("sheetViews"); writer.WriteStartElement("sheetView");
        writer.WriteAttributeString("workbookViewId", "0"); writer.WriteStartElement("pane");
        writer.WriteAttributeString("ySplit", "1"); writer.WriteAttributeString("topLeftCell", "A2");
        writer.WriteAttributeString("activePane", "bottomLeft"); writer.WriteAttributeString("state", "frozen");
        writer.WriteEndElement(); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("cols");
        for (int column = 0; column < Headers.Length; column++)
        {
            int longest = Headers[column].Length;
            foreach (var row in rows) longest = Math.Max(longest, CellText(row, column).Length);
            writer.WriteStartElement("col"); writer.WriteAttributeString("min", (column + 1).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("max", (column + 1).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("width", Math.Clamp(longest + 2, 8, 50).ToString(CultureInfo.InvariantCulture));
            writer.WriteAttributeString("customWidth", "1"); writer.WriteEndElement();
        }
        writer.WriteEndElement();
        writer.WriteStartElement("sheetData");
        WriteRow(writer, 1, Headers.Select((value, index) => (Value: (string?)value, Index: index)), null);
        int number = 1;
        foreach (var row in rows)
        {
            ct.ThrowIfCancellationRequested();
            if (++number > options.MaximumWorkbookRows) throw new InvalidDataException("Verification workbook row limit exceeded.");
            var cells = Enumerable.Range(0, Headers.Length).Select(index =>
                (Value: (string?)CellText(row, index), Index: index));
            WriteRow(writer, number, cells, row.Amount);
        }
        writer.WriteEndElement();
        writer.WriteStartElement("autoFilter", MainNs);
        writer.WriteAttributeString("ref", $"A1:I{number.ToString(CultureInfo.InvariantCulture)}");
        writer.WriteEndElement(); writer.WriteEndElement();
    }

    private static void WriteRow(XmlWriter writer, int rowNumber,
        IEnumerable<(string? Value, int Index)> cells, decimal? amount)
    {
        writer.WriteStartElement("row"); writer.WriteAttributeString("r", rowNumber.ToString(CultureInfo.InvariantCulture));
        foreach (var (value, index) in cells)
        {
            writer.WriteStartElement("c"); writer.WriteAttributeString("r", Column(index + 1) + rowNumber.ToString(CultureInfo.InvariantCulture));
            int style = rowNumber == 1 ? 1 : index == 7 ? 2 : 0;
            if (style > 0) writer.WriteAttributeString("s", style.ToString(CultureInfo.InvariantCulture));
            bool numberCell = rowNumber > 1 && (index == 0 || index == 5);
            string? numericText = index == 0 && rowNumber > 1 ? value
                : index == 5 && rowNumber > 1 && amount.HasValue ? amount.Value.ToString(CultureInfo.InvariantCulture) : null;
            if (numberCell && numericText is not null && SafeNumber(numericText, out string safe))
            {
                writer.WriteStartElement("v", MainNs); writer.WriteString(safe); writer.WriteEndElement();
            }
            else
            {
                writer.WriteAttributeString("t", "inlineStr"); writer.WriteStartElement("is");
                writer.WriteStartElement("t", MainNs); writer.WriteAttributeString("xml", "space", "http://www.w3.org/XML/1998/namespace", "preserve");
                writer.WriteString(value ?? ""); writer.WriteEndElement(); writer.WriteEndElement();
            }
            writer.WriteEndElement();
        }
        writer.WriteEndElement();
    }

    private static string CellText(VerificationWorkbookExportRow row, int index) => index switch
    {
        0 => row.SrNo.ToString(CultureInfo.InvariantCulture), 1 => row.Date, 2 => row.AccountCode,
        3 => row.AccountType, 4 => row.AccountName, 5 => row.Amount.ToString(CultureInfo.InvariantCulture),
        6 => row.Procedures, 7 => row.Observations, 8 => row.Status, _ => ""
    };

    private static bool SafeNumber(string value, out string safe)
    {
        safe = "";
        string text = value.Trim();
        if (text.Length is 0 or > 128 || !decimal.TryParse(text, NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint,
            CultureInfo.InvariantCulture, out _) || text.Count(char.IsAsciiDigit) > 15) return false;
        safe = text; return true;
    }

    private static string Column(int number)
    {
        var result = new StringBuilder();
        while (number > 0) { number--; result.Insert(0, (char)('A' + number % 26)); number /= 26; }
        return result.ToString();
    }

    private static void WriteStyles(XmlWriter writer)
    {
        writer.WriteStartElement("styleSheet", MainNs);
        writer.WriteStartElement("fonts"); writer.WriteAttributeString("count", "2");
        writer.WriteStartElement("font"); writer.WriteStartElement("sz"); writer.WriteAttributeString("val", "11"); writer.WriteEndElement();
        writer.WriteStartElement("name"); writer.WriteAttributeString("val", "Calibri"); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("font"); writer.WriteStartElement("b"); writer.WriteEndElement();
        writer.WriteStartElement("sz"); writer.WriteAttributeString("val", "11"); writer.WriteEndElement();
        writer.WriteStartElement("name"); writer.WriteAttributeString("val", "Calibri"); writer.WriteEndElement(); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("fills"); writer.WriteAttributeString("count", "2");
        writer.WriteStartElement("fill"); writer.WriteStartElement("patternFill"); writer.WriteAttributeString("patternType", "none"); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("fill"); writer.WriteStartElement("patternFill"); writer.WriteAttributeString("patternType", "gray125"); writer.WriteEndElement(); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("borders"); writer.WriteAttributeString("count", "1"); writer.WriteStartElement("border");
        foreach (string edge in new[] { "left", "right", "top", "bottom", "diagonal" }) writer.WriteElementString(edge, MainNs, "");
        writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("cellStyleXfs"); writer.WriteAttributeString("count", "1"); writer.WriteStartElement("xf");
        writer.WriteAttributeString("numFmtId", "0"); writer.WriteAttributeString("fontId", "0"); writer.WriteAttributeString("fillId", "0"); writer.WriteAttributeString("borderId", "0"); writer.WriteEndElement(); writer.WriteEndElement();
        writer.WriteStartElement("cellXfs"); writer.WriteAttributeString("count", "3");
        WriteXf(writer, 0, false); WriteXf(writer, 1, false); WriteXf(writer, 0, true);
        writer.WriteEndElement();
        writer.WriteStartElement("cellStyles"); writer.WriteAttributeString("count", "1"); writer.WriteStartElement("cellStyle");
        writer.WriteAttributeString("name", "Normal"); writer.WriteAttributeString("xfId", "0"); writer.WriteAttributeString("builtinId", "0"); writer.WriteEndElement();
        writer.WriteEndElement(); writer.WriteEndElement();
    }

    private static void WriteXf(XmlWriter writer, int font, bool wrap)
    {
        writer.WriteStartElement("xf"); writer.WriteAttributeString("numFmtId", "0");
        writer.WriteAttributeString("fontId", font.ToString(CultureInfo.InvariantCulture));
        writer.WriteAttributeString("fillId", "0"); writer.WriteAttributeString("borderId", "0"); writer.WriteAttributeString("xfId", "0");
        if (font != 0) writer.WriteAttributeString("applyFont", "1");
        if (wrap) { writer.WriteAttributeString("applyAlignment", "1"); writer.WriteStartElement("alignment"); writer.WriteAttributeString("wrapText", "1"); writer.WriteAttributeString("vertical", "top"); writer.WriteEndElement(); }
        writer.WriteEndElement();
    }

    private static void WriteEntry(ZipArchive archive, string name, Action<XmlWriter> write)
    {
        var entry = archive.CreateEntry(name, CompressionLevel.Fastest);
        using var stream = entry.Open(); using var writer = XmlWriter.Create(stream, new XmlWriterSettings
        { Encoding = new UTF8Encoding(false), CloseOutput = false, Indent = false });
        write(writer);
    }

    private sealed class CappedStream(int maximumBytes) : MemoryStream
    {
        private void Check(int count)
        {
            if (count < 0 || Math.Max(Length, Position + count) > maximumBytes)
                throw new InvalidDataException("Verification workbook exceeds its configured byte limit.");
        }
        public override void Write(byte[] buffer, int offset, int count) { Check(count); base.Write(buffer, offset, count); }
        public override void Write(ReadOnlySpan<byte> buffer) { Check(buffer.Length); base.Write(buffer); }
        public override void WriteByte(byte value) { Check(1); base.WriteByte(value); }
        public override void SetLength(long value)
        { if (value > maximumBytes) throw new InvalidDataException("Verification workbook exceeds its configured byte limit."); base.SetLength(value); }
    }
}
