using System.Data;
using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;
using NpgsqlTypes;

internal sealed record FetchRequest(int ClientId, int[] UploadIds, string PeriodStart,
    string PeriodEnd, int MaximumRows);

internal sealed record FetchResult(string Status, long Rows, long DataBytes,
    double FetchSeconds, double HashSeconds, string Sha256);

internal static class Program
{
    private const long MaximumDataBytes = 64L * 1024 * 1024;
    private const string Sql = """
        SELECT cd.id, cd.template_id, cd.upload_id, cd.version, cd.source_fingerprint,
               cd.extract_date::text, cd.extract_amount::double precision::text, cd.row_data::text
        FROM public.client_data cd
        WHERE cd.client_id=$1 AND cd.is_deleted IS FALSE AND cd.upload_id=ANY($2)
          AND cd.extract_date >= $3::date AND cd.extract_date < $4::date
        ORDER BY cd.extract_date NULLS LAST, cd.id
        LIMIT $5
        """;

    public static async Task<int> Main()
    {
        string? rawConnection = Environment.GetEnvironmentVariable("AUDIT_FETCH_BENCH_CONNECTION_STRING");
        if (string.IsNullOrWhiteSpace(rawConnection))
        {
            Console.Error.WriteLine("Database configuration is unavailable.");
            return 2;
        }

        try
        {
            var connection = new NpgsqlConnectionStringBuilder(ParseConnection(rawConnection))
            {
                Pooling = true,
                MinPoolSize = 0,
                MaxPoolSize = 1,
                Timeout = 10,
                CommandTimeout = 120,
                ApplicationName = "AuditPlatform.FetchBenchmark",
                LogParameters = false,
                IncludeErrorDetail = false
            };
            await using var dataSource = new NpgsqlDataSourceBuilder(connection.ConnectionString).Build();
            string? line;
            while ((line = await Console.In.ReadLineAsync()) is not null)
            {
                FetchRequest? request;
                try
                {
                    request = JsonSerializer.Deserialize<FetchRequest>(line,
                        new JsonSerializerOptions(JsonSerializerDefaults.Web));
                    Validate(request);
                    var result = await FetchAsync(dataSource, request!);
                    await Console.Out.WriteLineAsync(JsonSerializer.Serialize(result,
                        new JsonSerializerOptions(JsonSerializerDefaults.Web)));
                    await Console.Out.FlushAsync();
                }
                catch (Exception error)
                {
                    string reason = error.GetType().Name;
                    await Console.Out.WriteLineAsync(JsonSerializer.Serialize(
                        new { status = "error", reason }, new JsonSerializerOptions(JsonSerializerDefaults.Web)));
                    await Console.Out.FlushAsync();
                    return 1;
                }
            }
            return 0;
        }
        catch (Exception)
        {
            Console.Error.WriteLine("The C# read-only benchmark could not start.");
            return 1;
        }
    }

    private static string ParseConnection(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)
            || (uri.Scheme != "postgres" && uri.Scheme != "postgresql"))
            return value;

        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = uri.Host,
            Port = uri.IsDefaultPort ? 5432 : uri.Port,
            Database = Uri.UnescapeDataString(uri.AbsolutePath.TrimStart('/')),
            Username = Uri.UnescapeDataString(uri.UserInfo.Split(':', 2)[0]),
            Password = uri.UserInfo.Contains(':')
                ? Uri.UnescapeDataString(uri.UserInfo[(uri.UserInfo.IndexOf(':') + 1)..]) : ""
        };
        foreach (string pair in uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            string[] parts = pair.Split('=', 2);
            string key = Uri.UnescapeDataString(parts[0]);
            string val = parts.Length == 2 ? Uri.UnescapeDataString(parts[1]) : "";
            if (key.Equals("sslmode", StringComparison.OrdinalIgnoreCase)
                && Enum.TryParse<SslMode>(val, true, out var sslMode)) builder.SslMode = sslMode;
            else if (key.Equals("application_name", StringComparison.OrdinalIgnoreCase))
                builder.ApplicationName = val;
        }
        return builder.ConnectionString;
    }

    private static void Validate(FetchRequest? request)
    {
        if (request is null || request.ClientId < 1 || request.UploadIds is not { Length: > 0 and <= 10000 }
            || request.UploadIds.Any(id => id < 1) || request.MaximumRows is < 1 or > 100000)
            throw new InvalidOperationException();
        var start = DateOnly.ParseExact(request.PeriodStart, "yyyy-MM-dd", CultureInfo.InvariantCulture);
        var end = DateOnly.ParseExact(request.PeriodEnd, "yyyy-MM-dd", CultureInfo.InvariantCulture);
        if (start > end || end.DayNumber - start.DayNumber > 366) throw new InvalidOperationException();
    }

    private static async Task<FetchResult> FetchAsync(NpgsqlDataSource dataSource, FetchRequest request)
    {
        await using var connection = await dataSource.OpenConnectionAsync();
        await using var transaction = await connection.BeginTransactionAsync(IsolationLevel.RepeatableRead);
        await using (var setup = new NpgsqlCommand("SET TRANSACTION READ ONLY; SET LOCAL statement_timeout='120s'",
            connection, transaction)) await setup.ExecuteNonQueryAsync();

        await using var command = new NpgsqlCommand(Sql, connection, transaction);
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Integer, Value = request.ClientId });
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Integer, Value = request.UploadIds });
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Date,
            Value = DateOnly.ParseExact(request.PeriodStart, "yyyy-MM-dd", CultureInfo.InvariantCulture) });
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Date,
            Value = DateOnly.ParseExact(request.PeriodEnd, "yyyy-MM-dd", CultureInfo.InvariantCulture).AddDays(1) });
        command.Parameters.Add(new NpgsqlParameter { NpgsqlDbType = NpgsqlDbType.Integer, Value = request.MaximumRows + 1 });

        var rows = new List<string?[]>();
        long dataBytes = 0;
        var fetchTimer = Stopwatch.StartNew();
        await using (var reader = await command.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
            {
                var row = new string?[reader.FieldCount];
                for (int index = 0; index < reader.FieldCount; index++)
                {
                    row[index] = reader.IsDBNull(index) ? null
                        : index < 4 ? Convert.ToString(reader.GetValue(index), CultureInfo.InvariantCulture)
                        : reader.GetString(index);
                    if (row[index] is { } cell) dataBytes += Encoding.UTF8.GetByteCount(cell);
                }
                rows.Add(row);
                if (rows.Count > request.MaximumRows || dataBytes > MaximumDataBytes)
                    throw new InvalidOperationException();
            }
        }
        fetchTimer.Stop();

        var hashTimer = Stopwatch.StartNew();
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var row in rows)
        {
            foreach (string? cell in row)
            {
                byte[] bytes = cell is null ? [] : Encoding.UTF8.GetBytes(cell);
                byte[] length = BitConverter.GetBytes(cell is null ? uint.MaxValue : (uint)bytes.Length);
                hash.AppendData(length);
                hash.AppendData(bytes);
            }
        }
        string digest = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
        hashTimer.Stop();
        await transaction.RollbackAsync();
        return new("success", rows.Count, dataBytes, fetchTimer.Elapsed.TotalSeconds,
            hashTimer.Elapsed.TotalSeconds, digest);
    }
}
