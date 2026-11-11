using System.Diagnostics;
using System.Text;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Logging;
using AuditPlatform.LogRehearsal;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class LogReconciliationTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>, IAsyncLifetime
{
    private static int nextSourceRange = 1000000;
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private static JsonElement Json(object value) => JsonSerializer.SerializeToElement(value);
    private static LegacyLogRecord Audit(int id, string time = "2025-01-02T03:04:05Z") => new("audit_log", id,
        Json(new { user = "Admin", action = "Update", module = "Users", details = "password=PRIVATE_SENTINEL", timestamp = time }));
    private static ReconciliationManifest Manifest(params LegacyLogRecord[] records) => new()
    {
        SourceKind = "synthetic", OriginId = Guid.NewGuid(), SnapshotId = Guid.NewGuid(), Records = [.. records]
    };
    private Task<RehearsalTarget> TargetAsync(string? connection = null, string? owner = null) =>
        RehearsalTarget.OpenAsync(owner ?? fixture.OwnedRunDirectory, connection ?? fixture.ConnectionString, Token);
    public async ValueTask InitializeAsync() => await fixture.ApplyReconciliationSchemaAsync(Token);
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    [Fact]
    public async Task All_record_types_preserve_mappings_resolution_and_compact_evidence_without_source_mutation()
    {
        var canonical = await fixture.CreateSourceRowAsync(5, Token);
        var manifest = FullManifest(canonical);
        await using var target = await TargetAsync();
        var runner = new ReconciliationRunner(target);
        var first = await runner.RunAsync(manifest, cancellationToken: Token);
        Assert.Equal("completed", first.Status);
        Assert.Equal(4, first.Imported);
        Assert.True(first.Redactions > 0);
        Assert.All(first.Items, item => Assert.Equal(item.SourceId, item.TargetId));
        var second = await runner.RunAsync(manifest, cancellationToken: Token);
        Assert.Equal(0, second.Imported);
        Assert.Equal(4, second.Matched);
        Assert.All(second.Items, item => Assert.Equal("already_applied", item.Status));
        Assert.Equal(4L, await MapCountAsync(manifest.OriginId, Token));
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using (var source = new NpgsqlCommand("SELECT row_data::text FROM public.client_data WHERE id=$1", connection))
        {
            source.Parameters.AddWithValue(canonical);
            Assert.Equal("{}", await source.ExecuteScalarAsync(Token));
        }
        var dataId = first.Items.Single(item => item.Kind == "data_audit_log").TargetId!.Value;
        await using (var command = new NpgsqlCommand("SELECT old_row_data::text,new_row_data::text,actor_kind,actor_user_id FROM public.data_audit_log WHERE id=$1", connection))
        {
            command.Parameters.AddWithValue(dataId);
            await using var reader = await command.ExecuteReaderAsync(Token);
            Assert.True(await reader.ReadAsync(Token));
            using var old = JsonDocument.Parse(reader.GetString(0));
            using var current = JsonDocument.Parse(reader.GetString(1));
            // Both null sides must remain: this was an already-compacted delta.
            Assert.True(old.RootElement.TryGetProperty("added", out var before));
            Assert.True(current.RootElement.TryGetProperty("added", out var after));
            Assert.Equal(JsonValueKind.Null, before.ValueKind);
            Assert.Equal(JsonValueKind.Null, after.ValueKind);
            Assert.Equal(LogSanitizer.Redacted, current.RootElement.GetProperty("password").GetString());
            Assert.Equal("imported_unknown", reader.GetString(2));
            Assert.True(reader.IsDBNull(3));
        }
        var errorId = first.Items.Single(item => item.Kind == "error_log").TargetId!.Value;
        await using var error = new NpgsqlCommand("SELECT to_jsonb(t)::text FROM public.error_log t WHERE id=$1", connection);
        error.Parameters.AddWithValue(errorId);
        var stored = (string)(await error.ExecuteScalarAsync(Token))!;
        Assert.DoesNotContain("PRIVATE_SENTINEL", stored);
        using var errorJson = JsonDocument.Parse(stored);
        Assert.True(errorJson.RootElement.GetProperty("is_resolved").GetBoolean());
        Assert.Equal("Approved fixture resolution", errorJson.RootElement.GetProperty("resolution_notes").GetString());
        Assert.Equal(first.Items.Single(item => item.Kind == "error_codes").TargetId,
            errorJson.RootElement.GetProperty("error_code_id").GetInt32());
    }

    [Fact]
    public async Task Id_collision_allocates_a_mapping_and_preserves_unrelated_record_and_sequence()
    {
        const int sourceId = 700001;
        await ControlAsync("""
            INSERT INTO public.audit_log(id,"user",action,module,details) OVERRIDING SYSTEM VALUE
            VALUES (700001,'Existing','Keep','Users','KEEP_EXISTING')
            """, Token);
        var manifest = Manifest(Audit(sourceId));
        await using var target = await TargetAsync();
        var report = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token);
        Assert.Equal("completed", report.Status);
        var mapped = Assert.Single(report.Items).TargetId!.Value;
        Assert.NotEqual(sourceId, mapped);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using (var old = new NpgsqlCommand("SELECT details FROM public.audit_log WHERE id=700001", connection))
            Assert.Equal("KEEP_EXISTING", await old.ExecuteScalarAsync(Token));
        await using var next = new NpgsqlCommand("INSERT INTO public.audit_log(\"user\",action,module) VALUES ('New','Create','Users') RETURNING id", connection);
        Assert.True((int)(await next.ExecuteScalarAsync(Token))! > mapped);
    }

    [Fact]
    public async Task Same_registry_definition_can_map_two_origins_without_overwriting_the_registry()
    {
        var code = "FIX-" + Guid.NewGuid().ToString("N")[..12];
        var a = RegistryManifest(101, code, "Safe fixture message");
        var b = RegistryManifest(102, code, "Safe fixture message");
        await using var target = await TargetAsync();
        var runner = new ReconciliationRunner(target);
        var first = Assert.Single((await runner.RunAsync(a, cancellationToken: Token)).Items);
        var second = Assert.Single((await runner.RunAsync(b, cancellationToken: Token)).Items);
        Assert.Equal("matched", second.Status);
        Assert.Equal(first.TargetId, second.TargetId);
        Assert.Equal(1L, await MapCountAsync(a.OriginId, Token));
        Assert.Equal(1L, await MapCountAsync(b.OriginId, Token));
    }

    [Fact]
    public async Task Conflicting_registry_definitions_and_changed_source_or_target_are_reported_without_overwrite()
    {
        var code = "FIX-" + Guid.NewGuid().ToString("N")[..12];
        var first = RegistryManifest(201, code, "Original fixture message");
        await using var target = await TargetAsync();
        var runner = new ReconciliationRunner(target);
        var applied = await runner.RunAsync(first, cancellationToken: Token);
        var changed = RegistryManifest(201, code, "Different fixture message") with { OriginId = first.OriginId };
        Assert.Equal("source_changed", Assert.Single((await runner.RunAsync(changed, cancellationToken: Token)).Items).Reason);
        var foreign = RegistryManifest(201, code, "Different fixture message");
        Assert.Equal("registry_conflict", Assert.Single((await runner.RunAsync(foreign, cancellationToken: Token)).Items).Reason);
        var id = Assert.Single(applied.Items).TargetId!.Value;
        await ControlAsync($"UPDATE public.error_codes SET user_message='Later administrator edit' WHERE id={id}", Token);
        Assert.Equal("target_changed", Assert.Single((await runner.RunAsync(first, cancellationToken: Token)).Items).Reason);
        Assert.Equal(1L, await MapCountAsync(first.OriginId, Token));
    }

    [Theory]
    [InlineData("2025-01-02 08:04:05", "Asia/Karachi", "2025-01-02T03:04:05Z")]
    [InlineData("2025-01-02T08:04:05+05:00", null, "2025-01-02T03:04:05Z")]
    public async Task Explicit_time_rules_convert_to_utc_and_preserve_original_timestamp(string sourceTime, string? zone, string expected)
    {
        var manifest = Manifest(Audit(301, sourceTime));
        if (zone is not null) manifest.NaiveTimeZones["audit_log.timestamp"] = zone;
        await using var target = await TargetAsync();
        var row = Assert.Single((await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token)).Items);
        Assert.Equal("imported", row.Status);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("SELECT \"timestamp\",source_timestamp FROM public.audit_log WHERE id=$1", connection);
        command.Parameters.AddWithValue(row.TargetId!.Value);
        await using var reader = await command.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        Assert.Equal(DateTime.Parse(expected, null, System.Globalization.DateTimeStyles.AdjustToUniversal | System.Globalization.DateTimeStyles.AssumeUniversal), reader.GetDateTime(0));
        using var preserved = JsonDocument.Parse(reader.GetString(1));
        Assert.Equal(sourceTime, preserved.RootElement.GetProperty("timestamp").GetString());
    }

    [Theory]
    [InlineData("2025-01-02 03:04:05", null, "missing_timezone")]
    [InlineData("2025-11-02 01:30:00", "America/New_York", "ambiguous_time")]
    [InlineData("2025-03-09 02:30:00", "America/New_York", "ambiguous_time")]
    [InlineData("2025-01-02T03:04:05.1234567Z", null, "unsupported_time_precision")]
    public async Task Uncertain_or_lossy_timestamps_are_flagged_without_import(string time, string? zone, string reason)
    {
        var manifest = Manifest(Audit(401, time));
        if (zone is not null) manifest.NaiveTimeZones["audit_log.timestamp"] = zone;
        await using var target = await TargetAsync();
        var report = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token);
        Assert.Equal("conflicts", report.Status);
        Assert.Equal(reason, Assert.Single(report.Items).Reason);
        Assert.Equal(0L, await MapCountAsync(manifest.OriginId, Token));
    }

    [Fact]
    public async Task Unproven_canonical_and_registry_references_are_not_guessed()
    {
        var wrongClient = await fixture.CreateSourceRowAsync(6, Token);
        var manifest = FullManifest(wrongClient);
        await using var target = await TargetAsync();
        var report = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token);
        Assert.Contains(report.Items, item => item.Reason == "foreign_source_row");
        var error = manifest.Records.Single(r => r.Kind == "error_log");
        var missing = Manifest(error) with { ClientIds = new() { [1] = 5 } };
        var result = await new ReconciliationRunner(target).RunAsync(missing, cancellationToken: Token);
        Assert.Equal("missing_registry_map", Assert.Single(result.Items).Reason);
    }

    [Fact]
    public async Task Cancellation_between_records_resumes_from_verified_checkpoints()
    {
        var manifest = Manifest(Audit(501), Audit(502), Audit(503));
        await using var target = await TargetAsync();
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(Token);
        var progress = new InlineProgress(_ => cancellation.Cancel());
        var first = await new ReconciliationRunner(target).RunAsync(manifest, progress, cancellation.Token);
        Assert.Equal("cancelled", first.Status);
        Assert.Equal(1, first.Imported);
        Assert.Equal(2, first.Remaining);
        var resumed = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token);
        Assert.Equal(2, resumed.Imported);
        Assert.Equal(1, resumed.Matched);
        Assert.Equal(3L, await MapCountAsync(manifest.OriginId, Token));
    }

    [Fact]
    public async Task Cancelled_blocked_record_rolls_back_without_checkpoint()
    {
        var manifest = Manifest(Audit(601));
        await using var target = await TargetAsync();
        await using (var control = await fixture.OpenControlConnectionAsync(Token))
        await using (var hold = await control.BeginTransactionAsync(Token))
        {
            await using var command = new NpgsqlCommand("LOCK TABLE public.audit_log IN ACCESS EXCLUSIVE MODE", control, hold);
            await command.ExecuteNonQueryAsync(Token);
            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(Token);
            cancellation.CancelAfter(TimeSpan.FromMilliseconds(200));
            var report = await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: cancellation.Token);
            Assert.Equal("cancelled", report.Status);
            await hold.RollbackAsync(Token);
        }
        Assert.Equal(0L, await MapCountAsync(manifest.OriginId, Token));
        Assert.Equal(1, (await new ReconciliationRunner(target).RunAsync(manifest, cancellationToken: Token)).Imported);
    }

    [Fact]
    public async Task Lost_commit_acknowledgement_is_reconciled_on_explicit_resume_without_duplicate_insert()
    {
        var manifest = Manifest(Audit(701));
        await using var relay = fixture.CreateCommitProxy();
        var settings = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Port = relay.Port };
        await using (var throughRelay = await TargetAsync(settings.ConnectionString))
        {
            relay.Arm();
            var interrupted = await new ReconciliationRunner(throughRelay).RunAsync(manifest, cancellationToken: Token);
            Assert.Equal("unknown", interrupted.Status);
            Assert.Null(Assert.Single(interrupted.Items).TargetId);
            await relay.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5), Token);
        }
        Assert.Equal(1L, await MapCountAsync(manifest.OriginId, Token));
        await using var direct = await TargetAsync();
        var resumed = await new ReconciliationRunner(direct).RunAsync(manifest, cancellationToken: Token);
        Assert.Equal(0, resumed.Imported);
        Assert.Equal("already_applied", Assert.Single(resumed.Items).Status);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var count = new NpgsqlCommand("SELECT count(*) FROM public.audit_log WHERE source_origin=$1", connection);
        count.Parameters.AddWithValue(manifest.OriginId.ToString("D"));
        Assert.Equal(1L, await count.ExecuteScalarAsync(Token));
    }

    [Fact]
    public async Task Concurrent_runs_share_one_mapping_and_do_not_duplicate_records()
    {
        var manifest = Manifest(Audit(801), Audit(802));
        await using var a = await TargetAsync();
        await using var b = await TargetAsync();
        var reports = await Task.WhenAll(new ReconciliationRunner(a).RunAsync(manifest, cancellationToken: Token),
            new ReconciliationRunner(b).RunAsync(manifest, cancellationToken: Token));
        Assert.All(reports, report => Assert.Equal("completed", report.Status));
        Assert.Equal(2, reports.Sum(r => r.Imported));
        Assert.Equal(2L, await MapCountAsync(manifest.OriginId, Token));
    }

    [Theory]
    [InlineData("remote.example", "auditplatform_test", false)]
    [InlineData("127.0.0.1", "postgres", false)]
    [InlineData("127.0.0.1", "auditplatform_test", true)]
    public async Task Guard_rejects_external_hosts_roles_and_ordinary_database_names(string host, string user, bool wrongName)
    {
        var settings = new NpgsqlConnectionStringBuilder(fixture.ConnectionString) { Host = host, Username = user };
        if (wrongName) settings.Database = "postgres";
        await Assert.ThrowsAsync<InvalidOperationException>(() => TargetAsync(settings.ConnectionString));
    }

    [Fact]
    public async Task Guard_verifies_physical_cluster_directory_and_owned_input_paths()
    {
        await using var target = await TargetAsync();
        Assert.Throws<InvalidOperationException>(() => target.RequireOwnedInput(Path.Combine(RehearsalTarget.SolutionDirectory(), "source.json")));
        Assert.Throws<InvalidOperationException>(() => target.RequireOwnedInput(Path.Combine(fixture.OwnedRunDirectory, "input.txt")));
        var other = Path.Combine(Path.GetDirectoryName(fixture.OwnedRunDirectory)!, "run-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(other);
        await File.WriteAllTextAsync(Path.Combine(other, "fixture-owner"), Path.GetFileName(other)[4..], Token);
        await Assert.ThrowsAsync<InvalidOperationException>(() => TargetAsync(owner: other));
    }

    [Fact]
    public async Task Manifest_requires_synthetic_provenance_and_unique_source_ids_before_writing()
    {
        await using var target = await TargetAsync();
        var runner = new ReconciliationRunner(target);
        await Assert.ThrowsAsync<ArgumentException>(() => runner.RunAsync(Manifest(Audit(901)) with { SourceKind = "live" }, cancellationToken: Token));
        await Assert.ThrowsAsync<ArgumentException>(() => runner.RunAsync(Manifest(Audit(902), Audit(902)), cancellationToken: Token));
        Assert.Throws<ArgumentException>(() => ReconciliationJson.Parse(Encoding.UTF8.GetBytes("{\"sourceKind\":\"synthetic\",\"sourceKind\":\"live\"}")));
        Assert.Throws<JsonException>(() => ReconciliationJson.Parse(Encoding.UTF8.GetBytes("{\"unexpected\":true}")));
    }

    [Fact]
    public async Task Cli_reads_only_owned_json_and_emits_sanitized_counts_and_ids()
    {
        var manifest = Manifest(Audit(1001));
        var input = Path.Combine(fixture.OwnedRunDirectory, "synthetic-" + Guid.NewGuid().ToString("N") + ".json");
        await File.WriteAllBytesAsync(input, JsonSerializer.SerializeToUtf8Bytes(manifest, ReconciliationJson.Options), Token);
        var first = await RunCliAsync(input);
        Assert.Equal(0, first.ExitCode);
        Assert.DoesNotContain("PRIVATE_SENTINEL", first.Output);
        using var json = JsonDocument.Parse(first.Output);
        Assert.Equal(1, json.RootElement.GetProperty("imported").GetInt32());
        var repeated = await RunCliAsync(input);
        Assert.Equal(0, repeated.ExitCode);
        using var repeat = JsonDocument.Parse(repeated.Output);
        Assert.Equal(0, repeat.RootElement.GetProperty("imported").GetInt32());
        Assert.Equal(1, repeat.RootElement.GetProperty("matched").GetInt32());
        var rejected = await RunCliAsync(Path.Combine(RehearsalTarget.SolutionDirectory(), "PRIVATE_SENTINEL.json"));
        Assert.Equal(2, rejected.ExitCode);
        Assert.DoesNotContain("PRIVATE_SENTINEL", rejected.Output);
    }

    [Fact]
    public void Historical_payload_modes_preserve_existing_fingerprints_and_do_not_re_diff_null_deltas()
    {
        var sanitizer = new LogSanitizer(Options.Create(new LogPersistenceOptions()));
        using var delta = JsonDocument.Parse("{\"added\":null}");
        var compact = sanitizer.HistoricalCompact("UPDATE", delta.RootElement, delta.RootElement, new HashSet<string> { "added" }, false);
        Assert.Equal("{\"added\":null}", compact.Old);
        Assert.Equal(compact.Old, compact.New);
        var fingerprint = new string('a', 64);
        using var deleted = JsonDocument.Parse("{\"_row_fingerprint\":\"" + fingerprint + "\"}");
        Assert.Contains(fingerprint, sanitizer.HistoricalCompact("DELETE", deleted.RootElement, null, new HashSet<string>(), false).Old!);
        Assert.Throws<ArgumentException>(() => sanitizer.HistoricalCompact("DELETE", delta.RootElement, null, new HashSet<string>(), false));
        using var fullBefore = JsonDocument.Parse("{\"amount\":1,\"same\":2}");
        using var fullAfter = JsonDocument.Parse("{\"amount\":3,\"same\":2}");
        Assert.Equal("{\"amount\":1}", sanitizer.HistoricalCompact("UPDATE", fullBefore.RootElement, fullAfter.RootElement,
            new HashSet<string> { "amount", "same" }, true).Old);
    }

    private ReconciliationManifest FullManifest(int row)
    {
        var sourceId = Interlocked.Add(ref nextSourceRange, 1000);
        var code = "FIX-" + Guid.NewGuid().ToString("N")[..12];
        var registry = RegistryManifest(sourceId, code, "Safe fixture message").Records[0];
        return Manifest(registry, Audit(sourceId + 1),
            new("data_audit_log", sourceId + 2, Json(new
            {
                client_data_id = 88, user = "System", action = "UPDATE", changed_at = "2025-01-02T03:04:05Z",
                old_row_data = "{\"added\":null,\"amount\":200,\"password\":\"PRIVATE_SENTINEL\"}",
                new_row_data = "{\"added\":null,\"amount\":201,\"password\":\"PRIVATE_SENTINEL_CHANGED\"}"
            })),
            new("error_log", sourceId + 3, Json(new
            {
                error_code_id = sourceId, error_code = code, user_id = "Admin", client_id = 1,
                module = "Upload", status_code = 500, error_category = "System", error_message = "password=PRIVATE_SENTINEL",
                stack_trace = "PRIVATE_SENTINEL", input_data = "{\"password\":\"PRIVATE_SENTINEL\"}",
                is_resolved = 1, resolved_by = "Admin", resolution_notes = "Approved fixture resolution",
                resolved_at = "2025-01-03T03:04:05Z", created_at = "2025-01-02T03:04:05Z"
            }))) with
        {
            ClientIds = new() { [1] = 5 }, CanonicalRows = new() { [88] = new(row, 1) },
            ValueFields = new() { "added", "amount", "password" },
            PublicTextFields = new() { "error_codes.user_message", "error_codes.suggested_action", "error_log.resolution_notes" }
        };
    }

    private static ReconciliationManifest RegistryManifest(int id, string code, string message) => Manifest(
        new LegacyLogRecord("error_codes", id, Json(new
        {
            code, severity = "Error", category = "System", user_message = message,
            suggested_action = "Contact support.", is_active = 1, created_at = "2025-01-02T03:04:05Z"
        }))) with { PublicTextFields = new() { "error_codes.user_message", "error_codes.suggested_action" } };

    private async Task<(int ExitCode, string Output)> RunCliAsync(string input)
    {
        var root = RehearsalTarget.SolutionDirectory();
        var start = new ProcessStartInfo(Path.Combine(root, ".dotnet", "dotnet.exe"))
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        start.ArgumentList.Add(Path.Combine(root, "tools", "AuditPlatform.LogRehearsal", "bin", "Release", "net10.0", "AuditPlatform.LogRehearsal.dll"));
        foreach (var arg in new[] { "--input", input, "--owner-dir", fixture.OwnedRunDirectory }) start.ArgumentList.Add(arg);
        start.Environment["AUDIT_REHEARSAL_CONNECTION"] = fixture.ConnectionString;
        using var process = Process.Start(start)!;
        var output = process.StandardOutput.ReadToEndAsync(Token);
        var error = process.StandardError.ReadToEndAsync(Token);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(Token);
        timeout.CancelAfter(TimeSpan.FromSeconds(20));
        try { await process.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException) { process.Kill(entireProcessTree: true); throw; }
        return (process.ExitCode, (await output).Trim() + (await error).Trim());
    }

    private async Task<long> MapCountAsync(Guid origin, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var count = new NpgsqlCommand("SELECT count(*) FROM public.log_reconciliation_map WHERE source_origin=$1", connection);
        count.Parameters.AddWithValue(origin);
        return (long)(await count.ExecuteScalarAsync(token))!;
    }
    private async Task ControlAsync(string sql, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(token);
    }
    private sealed class InlineProgress(Action<ReconciliationProgress> action) : IProgress<ReconciliationProgress>
    {
        public void Report(ReconciliationProgress value) => action(value);
    }
}
