using System.Globalization;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using AuditPlatform.Api.Configuration;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Logging;

public sealed class LogSanitizer(IOptions<LogPersistenceOptions> options)
{
    public const string Redacted = "[redacted]";
    private readonly LogPersistenceOptions limits = options.Value;
    private static readonly Regex Sensitive = new(
        "password|passwd|secret|token|cookie|authorization|credential|connectionstring|connection_string|api[_-]?key|private[_-]?key|bearer|postgres(?:ql)?://|host\\s*=|sql\\s*=",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking,
        TimeSpan.FromMilliseconds(50));
    private static readonly Regex SensitiveText = new(
        "(?:password|passwd|secret|token|cookie|authorization|credential|api[_-]?key|private[_-]?key)\\s*[:=]|bearer\\s+|postgres(?:ql)?://|host\\s*=|sql\\s*=",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking,
        TimeSpan.FromMilliseconds(50));
    private static readonly HashSet<string> NumberKeys = new(StringComparer.Ordinal)
    {
        "record_id", "target_user_id", "upload_id", "template_id", "client_data_id",
        "row_count", "added", "updated", "deleted", "retry_count"
    };
    private static readonly HashSet<string> Outcomes = new(StringComparer.Ordinal)
        { "success", "failed", "rejected", "unknown", "cancelled" };
    private static readonly HashSet<string> Stages = new(StringComparer.Ordinal)
        { "request", "validation", "import", "commit", "audit", "diagnostic", "migration" };

    public string Text(string? value, int maximum, bool tail = false)
    {
        if (value is null)
            return "";
        // Oversized/unrecognizable text is omitted rather than partially scanned
        // or echoed by a fallback. Redaction always precedes display truncation.
        if (value.Length > 100000 || value.Any(c => c == '\0'))
            return Redacted;
        try { if (SensitiveText.IsMatch(value)) return Redacted; }
        catch (RegexMatchTimeoutException) { return Redacted; }
        var runes = value.EnumerateRunes().ToArray();
        return string.Concat((tail ? runes.TakeLast(maximum) : runes.Take(maximum)).Select(r => r.ToString()));
    }

    public string Label(string value, int maximum = 100)
    {
        if (string.IsNullOrWhiteSpace(value) || value.EnumerateRunes().Count() > maximum
            || value.Any(c => !char.IsLetterOrDigit(c) && c is not ('_' or '-' or '.' or ' ')))
            throw new ArgumentException("Logging label is missing or exceeds its permitted shape.");
        return value;
    }

    public void ValidateActor(LogActor actor)
    {
        if (actor.Kind is not ("authenticated" or "anonymous" or "system" or "imported_unknown")
            || (actor.Kind == "authenticated") != (actor.UserId is > 0)
            || (actor.Kind != "authenticated" && actor.UserId is not null))
            throw new ArgumentException("Logging actor must match its trusted identity kind.");
        if (string.IsNullOrWhiteSpace(actor.Name) || actor.Name.EnumerateRunes().Count() > 256 || actor.Name.Any(char.IsControl))
            throw new ArgumentException("Logging actor name is invalid.");
    }

    public string? Metadata(JsonElement? value, int? characterLimit = null)
    {
        if (value is null || value.Value.ValueKind == JsonValueKind.Null)
            return null;
        if (value.Value.ValueKind != JsonValueKind.Object)
            return "{\"_omitted\":true}";
        var allowed = new SortedDictionary<string, object?>(StringComparer.Ordinal);
        var omitted = false;
        foreach (var item in value.Value.EnumerateObject())
        {
            if (NumberKeys.Contains(item.Name) && item.Value.TryGetInt64Value(out var number) && number >= 0)
                allowed[item.Name] = number;
            else if (item.Name == "outcome" && item.Value.ValueKind == JsonValueKind.String
                && Outcomes.Contains(item.Value.GetString()!))
                allowed[item.Name] = item.Value.GetString();
            else if (item.Name == "stage" && item.Value.ValueKind == JsonValueKind.String
                && Stages.Contains(item.Value.GetString()!))
                allowed[item.Name] = item.Value.GetString();
            else
                omitted = true;
        }
        if (omitted)
            allowed["_omitted"] = true;
        var json = JsonSerializer.Serialize(allowed);
        return Encoding.UTF8.GetByteCount(json) <= limits.MetadataMaxBytes
            && (characterLimit is null || json.Length <= characterLimit)
            ? json : "{\"_truncated\":true}";
    }

    public (string? Old, string? New) Compact(DataChangeAudit record)
    {
        var oldRow = Object(record.OldRow);
        var newRow = Object(record.NewRow);
        switch (record.Event.Action.ToUpperInvariant())
        {
            case "INSERT": return (null, null);
            case "DELETE":
                if (oldRow.Count == 0) return (null, null);
                var canonical = PythonJson(record.OldRow!.Value);
                var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
                var fingerprint = "{\"_row_fingerprint\":\"" + hash + "\"}";
                if (Encoding.UTF8.GetByteCount(fingerprint) > limits.MetadataMaxBytes)
                    throw new ArgumentException("Compact audit fingerprint exceeds the permitted size.");
                return (fingerprint, null);
            case "UPDATE":
                var before = new SortedDictionary<string, object?>(StringComparer.Ordinal);
                var after = new SortedDictionary<string, object?>(StringComparer.Ordinal);
                foreach (var key in oldRow.Keys.Union(newRow.Keys))
                {
                    var hadOld = oldRow.TryGetValue(key, out var old);
                    var hasNew = newRow.TryGetValue(key, out var current);
                    if (hadOld && hasNew && PythonEqual(old, current)) continue;
                    var permitted = record.ValueFields.Contains(key) && !SensitiveKey(key);
                    before[key] = permitted ? SafeValue(hadOld ? old : default, 0) : Redacted;
                    after[key] = permitted ? SafeValue(hasNew ? current : default, 0) : Redacted;
                }
                return (Payload(before), Payload(after));
            default: return (null, null);
        }
    }

    // Historical deltas are already compact: diffing them again would erase
    // evidence such as an added/removed field represented by null on both sides.
    public (string? Old, string? New) HistoricalCompact(string action, JsonElement? oldRow,
        JsonElement? newRow, IReadOnlySet<string> valueFields, bool fullRows)
    {
        if (fullRows)
            return Compact(new(new(Guid.NewGuid(), Guid.NewGuid(), LogActor.System, null, action, "Rehearsal"),
                1, oldRow, newRow, valueFields));
        if (action == "INSERT") return (null, null);
        if (action == "DELETE")
        {
            var old = Object(oldRow);
            if (old.Count == 0) return (null, null);
            if (old.Count != 1 || !old.TryGetValue("_row_fingerprint", out var hash)
                || hash.ValueKind != JsonValueKind.String || hash.GetString()!.Length != 64
                || hash.GetString()!.Any(c => !char.IsAsciiHexDigit(c)) || Object(newRow).Count != 0)
                throw new ArgumentException("Historical DELETE requires an explicit fingerprint payload.");
            return (JsonSerializer.Serialize(new Dictionary<string, string> { ["_row_fingerprint"] = hash.GetString()! }), null);
        }
        if (action != "UPDATE")
        {
            if (Object(oldRow).Count != 0 || Object(newRow).Count != 0)
                throw new ArgumentException("Historical action has an unsupported payload.");
            return (null, null);
        }
        string? Side(JsonElement? source)
        {
            var result = new SortedDictionary<string, object?>(StringComparer.Ordinal);
            foreach (var pair in Object(source))
                result[pair.Key] = valueFields.Contains(pair.Key) && !SensitiveKey(pair.Key)
                    ? SafeValue(pair.Value, 0) : Redacted;
            return Payload(result);
        }
        return (Side(oldRow), Side(newRow));
    }

    private object? SafeValue(JsonElement value, int depth)
    {
        if (depth > limits.MetadataMaxDepth
            || (value.ValueKind == JsonValueKind.Array && value.GetArrayLength() > 100)
            || (value.ValueKind == JsonValueKind.Object && value.EnumerateObject().Count() > 100)
            || (value.ValueKind == JsonValueKind.String && value.GetString()!.EnumerateRunes().Count() > 1000)
            || (value.ValueKind == JsonValueKind.Number && IsFloat(value) && !double.IsFinite(value.GetDouble())))
            throw new ArgumentException("Compact audit value exceeds the permitted shape.");
        return value.ValueKind switch
        {
            JsonValueKind.Undefined or JsonValueKind.Null => null,
            JsonValueKind.String => Text(value.GetString(), 1000),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.Number => value.Clone(),
            JsonValueKind.Array => value.EnumerateArray().Select(v => SafeValue(v, depth + 1)).ToArray(),
            JsonValueKind.Object => value.EnumerateObject().ToDictionary(p => p.Name,
                p => SensitiveKey(p.Name) ? Redacted : SafeValue(p.Value, depth + 1)),
            _ => Redacted
        };
    }

    private string? Payload(SortedDictionary<string, object?> value)
    {
        if (value.Count == 0) return null;
        var json = JsonSerializer.Serialize(value);
        if (Encoding.UTF8.GetByteCount(json) > limits.MetadataMaxBytes)
            throw new ArgumentException("Compact audit exceeds the permitted size; the business change must be reviewed.");
        return json;
    }

    private static bool SensitiveKey(string key)
    {
        try { return Sensitive.IsMatch(key); }
        catch (RegexMatchTimeoutException)
        {
            // Do not attach the regex exception: it can retain the supplied
            // input. Required audit work must fail without leaking that input.
            throw new ArgumentException("Audit field policy could not be safely evaluated.");
        }
    }

    private static Dictionary<string, JsonElement> Object(JsonElement? value)
    {
        if (value is null || value.Value.ValueKind == JsonValueKind.Null) return new(StringComparer.Ordinal);
        if (value.Value.ValueKind != JsonValueKind.Object)
            throw new ArgumentException("Data audit requires JSON objects.");
        if (value.Value.GetRawText().Length > 100000 || value.Value.EnumerateObject().Count() > 1000)
            throw new ArgumentException("Data audit row exceeds its permitted size.");
        return value.Value.EnumerateObject().ToDictionary(p => p.Name, p => p.Value, StringComparer.Ordinal);
    }

    // The DELETE fingerprint follows Python json.dumps(sort_keys=True,
    // separators=(',', ':'), ensure_ascii=True). Raw rows are never persisted here.
    private string PythonJson(JsonElement value, int depth = 0)
    {
        if (depth > limits.MetadataMaxDepth)
            throw new ArgumentException("Audit JSON nesting exceeds its permitted depth.");
        return value.ValueKind switch
        {
            JsonValueKind.Object => "{" + string.Join(",", value.EnumerateObject()
                .OrderBy(p => p.Name, UnicodeComparer.Instance)
                .Select(p => Quote(p.Name) + ":" + PythonJson(p.Value, depth + 1))) + "}",
            JsonValueKind.Array => "[" + string.Join(",", value.EnumerateArray().Select(v => PythonJson(v, depth + 1))) + "]",
            JsonValueKind.String => Quote(value.GetString()!),
            JsonValueKind.Number => IsFloat(value) ? PythonFloat(value.GetDouble())
                : BigInteger.Parse(value.GetRawText(), CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture),
            JsonValueKind.True => "true", JsonValueKind.False => "false", JsonValueKind.Null => "null",
            _ => throw new ArgumentException("Unsupported audit JSON value.")
        };
    }

    private static string Quote(string text)
    {
        var output = new StringBuilder("\"");
        foreach (var c in text)
            output.Append(c switch
            {
                '"' => "\\\"", '\\' => "\\\\", '\b' => "\\b", '\f' => "\\f", '\n' => "\\n", '\r' => "\\r", '\t' => "\\t",
                < ' ' or > '~' => "\\u" + ((int)c).ToString("x4", CultureInfo.InvariantCulture), _ => c.ToString()
            });
        return output.Append('"').ToString();
    }

    private static string PythonFloat(double value)
    {
        if (!double.IsFinite(value)) throw new ArgumentException("Nonfinite audit values are not supported.");
        if (value == 0) return BitConverter.DoubleToInt64Bits(value) < 0 ? "-0.0" : "0.0";
        var sign = value < 0 ? "-" : "";
        var raw = Math.Abs(value).ToString("R", CultureInfo.InvariantCulture).ToLowerInvariant().Split('e');
        var exponent = raw.Length == 2 ? int.Parse(raw[1], CultureInfo.InvariantCulture) : 0;
        var point = raw[0].IndexOf('.');
        if (point < 0) point = raw[0].Length;
        var digits = raw[0].Replace(".", "", StringComparison.Ordinal);
        var first = digits.TakeWhile(c => c == '0').Count();
        exponent += point - first - 1;
        digits = digits[first..].TrimEnd('0');
        if (exponent is < -4 or >= 16)
            return sign + digits[0] + (digits.Length > 1 ? "." + digits[1..] : "")
                + "e" + (exponent < 0 ? "-" : "+") + Math.Abs(exponent).ToString("D2", CultureInfo.InvariantCulture);
        point = exponent + 1;
        return sign + (point <= 0 ? "0." + new string('0', -point) + digits
            : point >= digits.Length ? digits + new string('0', point - digits.Length) + ".0"
            : digits.Insert(point, "."));
    }

    private static bool IsFloat(JsonElement value) => value.GetRawText().IndexOfAny(['.', 'e', 'E']) >= 0;

    private static bool PythonEqual(JsonElement a, JsonElement b)
    {
        var numericA = a.ValueKind is JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False;
        var numericB = b.ValueKind is JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False;
        if (numericA && numericB)
        {
            var floatA = a.ValueKind == JsonValueKind.Number && IsFloat(a);
            var floatB = b.ValueKind == JsonValueKind.Number && IsFloat(b);
            if (floatA && floatB) return a.GetDouble() == b.GetDouble();
            static BigInteger Integer(JsonElement n) => n.ValueKind == JsonValueKind.True ? BigInteger.One
                : n.ValueKind == JsonValueKind.False ? BigInteger.Zero : BigInteger.Parse(n.GetRawText(), CultureInfo.InvariantCulture);
            if (!floatA && !floatB) return Integer(a) == Integer(b);
            var number = floatA ? a.GetDouble() : b.GetDouble();
            return double.IsFinite(number) && Math.Truncate(number) == number
                && new BigInteger(number) == Integer(floatA ? b : a);
        }
        if (a.ValueKind != b.ValueKind) return false;
        return a.ValueKind switch
        {
            JsonValueKind.Object => a.EnumerateObject().Count() == b.EnumerateObject().Count()
                && a.EnumerateObject().All(p => b.TryGetProperty(p.Name, out var other) && PythonEqual(p.Value, other)),
            JsonValueKind.Array => a.GetArrayLength() == b.GetArrayLength()
                && a.EnumerateArray().Zip(b.EnumerateArray()).All(p => PythonEqual(p.First, p.Second)),
            JsonValueKind.String => a.GetString() == b.GetString(), _ => true
        };
    }

    private sealed class UnicodeComparer : IComparer<string>
    {
        public static readonly UnicodeComparer Instance = new();
        public int Compare(string? x, string? y)
        {
            var a = (x ?? "").EnumerateRunes().Select(r => r.Value).ToArray();
            var b = (y ?? "").EnumerateRunes().Select(r => r.Value).ToArray();
            for (var i = 0; i < Math.Min(a.Length, b.Length); i++)
                if (a[i] != b[i]) return a[i].CompareTo(b[i]);
            return a.Length.CompareTo(b.Length);
        }
    }
}

internal static class LogJsonExtensions
{
    internal static bool TryGetInt64Value(this JsonElement value, out long number)
    {
        number = 0;
        return value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out number);
    }
}
