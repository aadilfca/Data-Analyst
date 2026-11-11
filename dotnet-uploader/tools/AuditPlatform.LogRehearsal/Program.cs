using System.Text.Json;

namespace AuditPlatform.LogRehearsal;

public static class RehearsalCommand
{
    public static async Task<int> Main(string[] args)
    {
        if (args.Length != 4 || args[0] != "--input" || args[2] != "--owner-dir")
        {
            Console.WriteLine("Use --input <synthetic.json> --owner-dir <owned fixture directory>. Credentials use AUDIT_REHEARSAL_CONNECTION.");
            return 2;
        }
        try
        {
            var connection = Environment.GetEnvironmentVariable("AUDIT_REHEARSAL_CONNECTION")
                ?? throw new InvalidOperationException("Explicit rehearsal settings are required.");
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(60));
            await using var target = await RehearsalTarget.OpenAsync(args[3], connection, deadline.Token);
            var input = target.RequireOwnedInput(args[1]);
            if (new FileInfo(input).Length > 5 * 1024 * 1024) throw new ArgumentException("Input is too large.");
            var manifest = ReconciliationJson.Parse(await File.ReadAllBytesAsync(input, deadline.Token));
            var report = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: deadline.Token);
            Console.WriteLine(JsonSerializer.Serialize(report, ReconciliationJson.Options));
            return report.Status == "completed" && report.Conflicts == 0 ? 0 : 3;
        }
        catch (Exception error) when (error is not OutOfMemoryException and not StackOverflowException)
        {
            // Paths, source fields, credentials and SQL exception details never
            // appear in CLI errors. The protected fixture retains its inputs.
            Console.WriteLine("{\"status\":\"rejected\",\"reason\":\"invalid_input_or_unavailable_owned_fixture\"}");
            return 2;
        }
    }
}
