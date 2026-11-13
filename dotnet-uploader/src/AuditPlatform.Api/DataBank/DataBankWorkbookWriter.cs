using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Xml;
using AuditPlatform.Api.Configuration;

namespace AuditPlatform.Api.DataBank;

// Request-local OOXML over framework ZIP/XML. No formulas, external links,
// spreadsheet-number narrowing, partial workbooks or temporary production files.
internal static class DataBankWorkbookWriter
{
    private const string Ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    private sealed record Cell(string? Value, string Type = "inlineStr");
    internal static byte[] Write(DataBankWorkbook input, DataBankExportOptions options, CancellationToken ct)
    {
        if (input.Current.Count + input.History.Count > options.MaximumRows) throw new InvalidDataException("Row capacity exceeded.");
        var keys = input.Template.Columns.Select(c => c.StorageKey).ToList(); var known = keys.ToHashSet(StringComparer.Ordinal);
        var extra = new SortedSet<string>(StringComparer.Ordinal);
        foreach (string raw in input.Current.Select(r => r.Raw).Concat(input.History.Select(r => r.Raw)))
        {
            ct.ThrowIfCancellationRequested(); using var json = JsonDocument.Parse(raw);
            if (json.RootElement.ValueKind != JsonValueKind.Object) throw new InvalidDataException("Unsupported row.");
            foreach (var field in json.RootElement.EnumerateObject()) if (!known.Contains(field.Name)) extra.Add(field.Name);
        }
        keys.AddRange(extra); if (keys.Count + 12 > 16384) throw new InvalidDataException("Worksheet column capacity exceeded.");
        var names = input.Template.Columns.ToDictionary(c => c.StorageKey, c => c.Name, StringComparer.Ordinal);
        string[] headings = keys.Select(key => names.TryGetValue(key, out string? name) ? $"{name} [{key}]" : key).ToArray();
        using var output = new CappedMemory(options.MaximumWorkbookBytes); long remaining = options.MaximumExpandedBytes;
        using (var zip = new ZipArchive(output, ZipArchiveMode.Create, true, Encoding.UTF8))
        {
            void Entry(string path, Action<XmlWriter> write)
            {
                var entry = zip.CreateEntry(path, CompressionLevel.Fastest); entry.LastWriteTime = new(2000, 1, 1, 0, 0, 0, TimeSpan.Zero);
                using var stream = entry.Open(); using var budget = new BudgetStream(stream, n => { remaining -= n; ct.ThrowIfCancellationRequested(); if (remaining < 0) throw new InvalidDataException("Expanded workbook capacity exceeded."); });
                using var writer = XmlWriter.Create(budget, new XmlWriterSettings { Encoding = new UTF8Encoding(false), CheckCharacters = true });
                try { write(writer); } catch (ArgumentException error) { throw new InvalidDataException("Unsupported XML cell text.", error); }
            }
            int count = input.Backup ? 2 : 1;
            Entry("[Content_Types].xml", w =>
            {
                w.WriteStartElement("Types", "http://schemas.openxmlformats.org/package/2006/content-types");
                void Type(string element, string key, string name, string type) { w.WriteStartElement(element); w.WriteAttributeString(key, name); w.WriteAttributeString("ContentType", type); w.WriteEndElement(); }
                Type("Default", "Extension", "rels", "application/vnd.openxmlformats-package.relationships+xml"); Type("Default", "Extension", "xml", "application/xml");
                Type("Override", "PartName", "/xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");
                for (int i = 1; i <= count; i++) Type("Override", "PartName", $"/xl/worksheets/sheet{i}.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");
                w.WriteEndElement();
            });
            Entry("_rels/.rels", w => { w.WriteStartElement("Relationships", "http://schemas.openxmlformats.org/package/2006/relationships"); Relationship(w, "rId1", "officeDocument", "xl/workbook.xml"); w.WriteEndElement(); });
            Entry("xl/workbook.xml", w =>
            {
                w.WriteStartElement("workbook", Ns); w.WriteStartElement("sheets");
                for (int i = 1; i <= count; i++) { w.WriteStartElement("sheet"); w.WriteAttributeString("name", i == 1 ? "Data" : "Changed-Deleted History"); w.WriteAttributeString("sheetId", i.ToString(CultureInfo.InvariantCulture)); w.WriteAttributeString("r", "id", "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "rId" + i); w.WriteEndElement(); }
                w.WriteEndElement(); w.WriteEndElement();
            });
            Entry("xl/_rels/workbook.xml.rels", w => { w.WriteStartElement("Relationships", "http://schemas.openxmlformats.org/package/2006/relationships"); for (int i = 1; i <= count; i++) Relationship(w, "rId" + i, "worksheet", $"worksheets/sheet{i}.xml"); w.WriteEndElement(); });
            IEnumerable<Cell[]> Current()
            {
                foreach (var row in input.Current)
                {
                    ct.ThrowIfCancellationRequested(); using var raw = JsonDocument.Parse(row.Raw);
                    yield return [Number(row.Id.ToString(CultureInfo.InvariantCulture)), new(row.Account), new(row.AccountName), ..keys.Select(k => Value(raw.RootElement, k)), new(row.Raw), new(row.Metadata)];
                }
            }
            IEnumerable<Cell[]> History()
            {
                foreach (var row in input.History)
                {
                    ct.ThrowIfCancellationRequested(); using var raw = JsonDocument.Parse(row.Raw);
                    yield return [Number(row.Id.ToString(CultureInfo.InvariantCulture)), Number(row.RowId.ToString(CultureInfo.InvariantCulture)), new(row.Action), new(row.At), new(row.Actor),
                        Number(row.SourceUpload?.ToString(CultureInfo.InvariantCulture)), Number(row.ReplacingUpload?.ToString(CultureInfo.InvariantCulture)), new(row.Account), new(row.AccountName),
                        ..keys.Select(k => Value(raw.RootElement, k)), new(row.Changes), new(row.Raw), new(row.Metadata)];
                }
            }
            Entry("xl/worksheets/sheet1.xml", w => Sheet(w, input, "Current data", ["Record ID", "Account Code", "Account Name", ..headings, "Source row JSON", "Stored metadata JSON"], Current(), input.Current.Count, ct));
            if (input.Backup) Entry("xl/worksheets/sheet2.xml", w => Sheet(w, input, "Recorded changed/deleted history",
                ["History ID", "Record ID", "Action", "Changed At", "Changed By", "Source Upload", "Replacing Upload", "Account Code", "Account Name", ..headings, "Changed Fields", "Source row JSON", "Stored metadata JSON"], History(), input.History.Count, ct));
        }
        return output.ToArray();
    }
    private static Cell Value(JsonElement row, string key)
    {
        if (!row.TryGetProperty(key, out var value) || value.ValueKind == JsonValueKind.Null) return new(null);
        return value.ValueKind switch { JsonValueKind.Number => Number(value.GetRawText()), JsonValueKind.String => new(value.GetString()),
            JsonValueKind.True => new("1", "b"), JsonValueKind.False => new("0", "b"), _ => new(value.GetRawText()) };
    }
    private static Cell Number(string? value)
    {
        if (value is null) return new(null);
        int digits = value.Split('e', 'E')[0].Count(char.IsDigit);
        return digits <= 15 && double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out double n) && double.IsFinite(n)
            && (Math.Abs(n) >= 1e-307 || n == 0 && !value.Split('e', 'E')[0].Any(c => c is >= '1' and <= '9')) ? new(value, "n") : new(value);
    }
    private static void Relationship(XmlWriter w, string id, string type, string target)
    { w.WriteStartElement("Relationship"); w.WriteAttributeString("Id", id); w.WriteAttributeString("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/" + type); w.WriteAttributeString("Target", target); w.WriteEndElement(); }
    private static string Column(int n) { string text = ""; while (n > 0) { n--; text = (char)('A' + n % 26) + text; n /= 26; } return text; }
    private static void Sheet(XmlWriter w, DataBankWorkbook input, string title, string[] headers, IEnumerable<Cell[]> rows, int count, CancellationToken ct)
    {
        if (headers.Distinct(StringComparer.Ordinal).Count() != headers.Length) throw new InvalidDataException("Ambiguous workbook headers require review.");
        w.WriteStartElement("worksheet", Ns); w.WriteStartElement("sheetViews"); w.WriteStartElement("sheetView"); w.WriteAttributeString("workbookViewId", "0");
        w.WriteStartElement("pane"); w.WriteAttributeString("ySplit", "5"); w.WriteAttributeString("topLeftCell", "A6"); w.WriteAttributeString("state", "frozen"); w.WriteEndElement(); w.WriteEndElement(); w.WriteEndElement();
        w.WriteStartElement("sheetData"); int index = 0;
        void Row(Cell[] cells)
        {
            ct.ThrowIfCancellationRequested(); w.WriteStartElement("row"); w.WriteAttributeString("r", (++index).ToString(CultureInfo.InvariantCulture));
            for (int i = 0; i < cells.Length; i++)
            {
                var cell = cells[i]; if (cell.Value is null) continue;
                if (cell.Value.Length > 32767) throw new InvalidDataException("Complete cell exceeds Excel capacity.");
                w.WriteStartElement("c"); w.WriteAttributeString("r", Column(i + 1) + index); w.WriteAttributeString("t", cell.Type);
                if (cell.Type == "inlineStr")
                {
                    w.WriteStartElement("is"); w.WriteStartElement("t"); w.WriteAttributeString("xml", "space", "http://www.w3.org/XML/1998/namespace", "preserve");
                    w.WriteString(LiteralText(cell.Value)); w.WriteEndElement(); w.WriteEndElement();
                }
                else w.WriteElementString("v", cell.Value); w.WriteEndElement();
            }
            w.WriteEndElement();
        }
        var q = input.Query;
        string scope = input.Backup ? "All dates / all accounts / complete current and recorded history" : $"{q.Start} through {q.End}; account: {q.Account ?? "not an account-ledger selection"}; search: {q.Search ?? "none"}; minimum: {q.Minimum?.ToString(CultureInfo.InvariantCulture) ?? "none"}; maximum: {q.Maximum?.ToString(CultureInfo.InvariantCulture) ?? "none"}; Sundays only: {q.Sunday}; quality: {q.Quality ?? "all"}";
        Row([new(title + " — " + input.Template.Name)]); Row([new($"Client {q.ClientId} / Template {input.Template.Id} / {count} records")]);
        Row([new("Captured UTC: " + input.CapturedAt.ToString("O", CultureInfo.InvariantCulture))]); Row([new(scope)]); Row(headers.Select(h => new Cell(h)).ToArray());
        foreach (var row in rows) Row(row);
        w.WriteEndElement(); w.WriteStartElement("autoFilter"); w.WriteAttributeString("ref", $"A5:{Column(headers.Length)}{Math.Max(5, index)}"); w.WriteEndElement(); w.WriteEndElement();
    }
    private static string LiteralText(string text)
    {
        if (!text.Contains("_x", StringComparison.Ordinal)) return text;
        var result = new StringBuilder(text.Length);
        for (int i = 0; i < text.Length; i++)
        {
            bool escape = text[i] == '_' && i + 6 < text.Length && text[i + 1] == 'x' && text[i + 6] == '_';
            if (escape) for (int n = i + 2; n < i + 6; n++) escape &= char.IsAsciiHexDigit(text[n]);
            result.Append(escape ? "_x005F_" : text[i].ToString());
        }
        return result.ToString();
    }
    private sealed class CappedMemory(int maximum) : MemoryStream
    {
        public override void Write(byte[] buffer, int offset, int count) { Check(count); base.Write(buffer, offset, count); }
        public override void Write(ReadOnlySpan<byte> buffer) { Check(buffer.Length); base.Write(buffer); }
        public override void WriteByte(byte value) { Check(1); base.WriteByte(value); }
        private void Check(int count) { if (Math.Max(Length, Position + count) > maximum) throw new InvalidDataException("Workbook capacity exceeded."); }
    }
    private sealed class BudgetStream(Stream target, Action<int> count) : Stream
    {
        public override bool CanRead => false; public override bool CanSeek => false; public override bool CanWrite => true;
        public override long Length => throw new NotSupportedException(); public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() => target.Flush(); public override void Write(byte[] buffer, int offset, int length) { count(length); target.Write(buffer, offset, length); }
        public override void Write(ReadOnlySpan<byte> buffer) { count(buffer.Length); target.Write(buffer); }
        public override int Read(byte[] buffer, int offset, int length) => throw new NotSupportedException(); public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException(); public override void SetLength(long value) => throw new NotSupportedException();
    }
}
