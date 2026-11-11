using System.Security.Claims;
using System.Text.Json;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Logging;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class AuditPersistenceTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>, IAsyncLifetime
{
    private static CancellationToken Token => TestContext.Current.CancellationToken;
    private static readonly IOptions<LogPersistenceOptions> Limits = Options.Create(new LogPersistenceOptions());
    private static LogSanitizer Sanitizer => new(Limits);
    private static PostgresAuditWriter Writer => new(Sanitizer, Limits);
    private static JsonElement Json(string value) => JsonDocument.Parse(value).RootElement.Clone();
    private static AuditEvent Event(string action = "Update", string module = "Users", int? client = null) =>
        new(Guid.NewGuid(), Guid.NewGuid(), new("authenticated", 7, "Aadil"), client, action, module,
            Json("{\"target_user_id\":12,\"password\":\"PRIVATE_SENTINEL\"}"), Guid.NewGuid().ToString("N"));
    private static PostgresTransactions Transactions(PostgresDataSourceFactory pool) =>
        new(pool, Options.Create(new PostgresOptions { CommandTimeoutSeconds = 3 }));

    public async ValueTask InitializeAsync() => await fixture.ApplyAuditSchemaAsync(Token);
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    [Fact]
    public async Task Required_audit_and_business_change_commit_together_with_trusted_actor()
    {
        var row = await fixture.CreateSourceRowAsync(5, Token);
        var record = Event(client: 5);
        await using var pool = fixture.CreatePool();
        var id = await Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await ChangeSourceAsync(context, row, token);
            var auditId = await Writer.WriteAsync(context, record, token);
            Assert.Equal(0L, await CountEventAsync(record.EventId, token));
            return auditId;
        }, Token);
        Assert.True(id > 0);
        Assert.Equal(1L, await CountEventAsync(record.EventId, Token));
        Assert.True(await SourceChangedAsync(row, Token));
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("SELECT actor_user_id,actor_kind,\"user\",details,client_id,operation_id FROM public.audit_log WHERE id=$1", connection);
        command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        Assert.Equal(7, reader.GetInt32(0));
        Assert.Equal("authenticated", reader.GetString(1));
        Assert.Equal("Aadil", reader.GetString(2));
        Assert.DoesNotContain("PRIVATE_SENTINEL", reader.GetString(3));
        Assert.Equal(5, reader.GetInt32(4));
        Assert.Equal(record.OperationId, reader.GetGuid(5));
    }

    [Fact]
    public async Task Failed_required_audit_rolls_back_the_business_change()
    {
        var row = await fixture.CreateSourceRowAsync(5, Token);
        await ControlAsync("ALTER TABLE public.audit_log ADD CONSTRAINT p05b_reject CHECK(module <> 'FailAudit')", Token);
        try
        {
            await using var pool = fixture.CreatePool();
            var record = Event(module: "FailAudit", client: 5);
            var error = await Assert.ThrowsAsync<PostgresException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
            {
                await ChangeSourceAsync(context, row, token);
                return await Writer.WriteAsync(context, record, token);
            }, Token));
            Assert.Equal(PostgresErrorCodes.CheckViolation, error.SqlState);
            Assert.False(await SourceChangedAsync(row, Token));
            Assert.Equal(0L, await CountEventAsync(record.EventId, Token));
        }
        finally { await ControlAsync("ALTER TABLE public.audit_log DROP CONSTRAINT p05b_reject", Token); }
    }

    [Fact]
    public async Task Duplicate_event_is_rejected_without_replaying_a_business_change()
    {
        var record = Event();
        await using var pool = fixture.CreatePool();
        var transactions = Transactions(pool);
        await transactions.ExecuteAsync((context, token) => Writer.WriteAsync(context, record, token), Token);
        var row = await fixture.CreateSourceRowAsync(5, Token);
        await Assert.ThrowsAsync<PostgresException>(() => transactions.ExecuteAsync(async (context, token) =>
        {
            await ChangeSourceAsync(context, row, token);
            return await Writer.WriteAsync(context, record, token);
        }, Token));
        Assert.False(await SourceChangedAsync(row, Token));
        Assert.Equal(1L, await CountEventAsync(record.EventId, Token));
    }

    [Fact]
    public async Task Data_change_preserves_compact_values_and_source_scope()
    {
        var row = await fixture.CreateSourceRowAsync(5, Token);
        await using var pool = fixture.CreatePool();
        var record = new DataChangeAudit(Event("UPDATE", client: 5), row,
            Json("{\"amount\":200,\"blank\":null,\"name\":\"old\",\"password\":\"PRIVATE_OLD\"}"),
            Json("{\"amount\":200.0,\"blank\":0,\"name\":\"new\",\"password\":\"PRIVATE_NEW\"}"),
            new HashSet<string> { "amount", "blank", "name", "password" });
        var id = await Transactions(pool).ExecuteAsync((context, token) => Writer.WriteDataChangeAsync(context, record, token), Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("SELECT old_row_data::text,new_row_data::text,client_id FROM public.data_audit_log WHERE id=$1", connection);
        command.Parameters.AddWithValue(id);
        await using var reader = await command.ExecuteReaderAsync(Token);
        Assert.True(await reader.ReadAsync(Token));
        var old = Json(reader.GetString(0));
        var current = Json(reader.GetString(1));
        Assert.False(old.TryGetProperty("amount", out _));
        Assert.Equal(JsonValueKind.Null, old.GetProperty("blank").ValueKind);
        Assert.Equal(0, current.GetProperty("blank").GetInt32());
        Assert.Equal("old", old.GetProperty("name").GetString());
        Assert.Equal("new", current.GetProperty("name").GetString());
        Assert.Equal(LogSanitizer.Redacted, current.GetProperty("password").GetString());
        Assert.DoesNotContain("PRIVATE_", reader.GetString(0) + reader.GetString(1));
        Assert.Equal(5, reader.GetInt32(2));
    }

    [Fact]
    public async Task Data_change_rejects_a_foreign_client_before_writing()
    {
        var row = await fixture.CreateSourceRowAsync(5, Token);
        var record = new DataChangeAudit(Event("DELETE", client: 6), row, Json("{\"amount\":10}"), null, new HashSet<string>());
        await using var pool = fixture.CreatePool();
        await Assert.ThrowsAsync<InvalidOperationException>(() => Transactions(pool).ExecuteAsync(
            (context, token) => Writer.WriteDataChangeAsync(context, record, token), Token));
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("SELECT count(*) FROM public.data_audit_log WHERE event_id=$1", connection);
        command.Parameters.AddWithValue(record.Event.EventId);
        Assert.Equal(0L, await command.ExecuteScalarAsync(Token));
    }

    [Theory]
    [InlineData("{\"a\":1,\"b\":\"text\"}", "bf8fb1bd9b79117d7175589d87275d4b9feafa3b2c0ff002242f30806115f020")]
    [InlineData("{\"é\":\"اردو\",\"\ue000\":1,\"\ud800\udc00\":2}", "2f3c2c22764ddda781b31ce94cebe4c8cd857c5450237f94c5202c91fc81f022")]
    [InlineData("{\"x\":-0.0,\"y\":1e-05,\"z\":1e+16}", "a5c48bccfaad6160a8268ae81bcc0e506b42d344277a9c48efa8419fa44670bf")]
    [InlineData("{\"x\":1.0,\"a\":[true,null,{\"é\":\"\\n\\t/\"}]}", "2134be974648b280c206f18639ed2b15472feca1c54fc35612a6fe50e805fa18")]
    [InlineData("{\"x\":9007199254740993,\"y\":1.7976931348623157e308}", "61871f08b626d558de582f3c854d65fc77cc62eb7e2971b3e4c08dd1db3e4813")]
    [InlineData("{\"x\":0.0001,\"y\":1000000000000000.0,\"z\":5e-324}", "9ef3922a5f89def419eb57d6859b7484ab85368e5f491f7368695d7832ad3aef")]
    public void Delete_fingerprints_match_independently_generated_python_vectors(string source, string expected)
    {
        var result = Sanitizer.Compact(new(Event("DELETE"), 1, Json(source), null, new HashSet<string>()));
        Assert.Equal(expected, Json(result.Old!).GetProperty("_row_fingerprint").GetString());
        Assert.Null(result.New);
    }

    [Fact]
    public void Insert_empty_delete_and_unchanged_updates_keep_no_payload()
    {
        foreach (var action in new[] { "INSERT", "DELETE", "OTHER" })
            Assert.Equal((null, null), Sanitizer.Compact(new(Event(action), 1, Json("{}"), Json("{\"ignored\":1}"), new HashSet<string>())));
        Assert.Equal((null, null), Sanitizer.Compact(new(Event("UPDATE"), 1,
            Json("{\"a\":true,\"b\":[1,{\"a\":200}]}"),
            Json("{\"a\":1,\"b\":[true,{\"a\":200.0}]}"), new HashSet<string> { "a", "b" })));
    }

    [Fact]
    public void Missing_fields_differ_from_null_and_large_integer_differences_survive()
    {
        var record = new DataChangeAudit(Event("UPDATE"), 1,
            Json("{\"a\":9007199254740993,\"removed\":null}"),
            Json("{\"a\":9007199254740992.0,\"added\":null}"),
            new HashSet<string> { "a", "removed", "added" });
        var result = Sanitizer.Compact(record);
        Assert.Equal(3, Json(result.Old!).EnumerateObject().Count());
        Assert.Equal(3, Json(result.New!).EnumerateObject().Count());
        Assert.Equal("9007199254740993", Json(result.Old!).GetProperty("a").GetRawText());
    }

    [Fact]
    public async Task Reapplying_schema_preserves_historical_identity_text_and_registry_values()
    {
        await ControlAsync("""
            INSERT INTO public.audit_log(id,"user",action,module,details,"timestamp") OVERRIDING SYSTEM VALUE
            VALUES (70001,'Admin','Legacy','Users','legacy details','2025-01-02 03:04:05+00');
            UPDATE public.error_codes SET user_message='Site-specific safe message' WHERE code='ERR-UNK-001'
            """, Token);
        await fixture.ApplyAuditSchemaAsync(Token);
        await fixture.ApplyAuditSchemaAsync(Token);
        await using var connection = await fixture.OpenControlConnectionAsync(Token);
        await using var command = new NpgsqlCommand("SELECT \"user\",details,actor_kind,source_origin FROM public.audit_log WHERE id=70001", connection);
        await using (var reader = await command.ExecuteReaderAsync(Token))
        {
            Assert.True(await reader.ReadAsync(Token));
            Assert.Equal("Admin", reader.GetString(0));
            Assert.Equal("legacy details", reader.GetString(1));
            Assert.True(reader.IsDBNull(2));
            Assert.True(reader.IsDBNull(3));
        }
        await using var registry = new NpgsqlCommand("SELECT user_message FROM public.error_codes WHERE code='ERR-UNK-001'", connection);
        Assert.Equal("Site-specific safe message", await registry.ExecuteScalarAsync(Token));
    }

    [Fact]
    public async Task Schema_refuses_incompatible_types_and_rolls_back_partial_ddl()
    {
        await using var connection = await fixture.OpenEmptyOwnedDatabaseAsync(Token);
        await using (var setup = new NpgsqlCommand("""
            CREATE TABLE public.client_data(id integer PRIMARY KEY);
            CREATE TABLE public.audit_log(id integer PRIMARY KEY,"user" text,action text,module text,details text,"timestamp" timestamp)
            """, connection)) await setup.ExecuteNonQueryAsync(Token);
        await using (var transaction = await connection.BeginTransactionAsync(Token))
        {
            await using var command = new NpgsqlCommand(await PostgresFixture.ReadAuditSchemaAsync(Token), connection, transaction);
            await Assert.ThrowsAsync<PostgresException>(() => command.ExecuteNonQueryAsync(Token));
            await transaction.RollbackAsync(Token);
        }
        await using var verify = new NpgsqlCommand("SELECT to_regclass('public.error_log') IS NULL", connection);
        Assert.True((bool)(await verify.ExecuteScalarAsync(Token))!);
    }

    [Fact]
    public async Task Schema_requires_the_canonical_table_and_actor_constraints_reject_false_identity()
    {
        await using (var empty = await fixture.OpenEmptyOwnedDatabaseAsync(Token))
        {
            await using var command = new NpgsqlCommand(await PostgresFixture.ReadAuditSchemaAsync(Token), empty);
            await Assert.ThrowsAsync<PostgresException>(() => command.ExecuteNonQueryAsync(Token));
        }
        foreach (var values in new[] { "NULL,7", "'authenticated',NULL", "'anonymous',7", "'invented',NULL" })
            await Assert.ThrowsAsync<PostgresException>(() => ControlAsync(
                "INSERT INTO public.audit_log(\"user\",action,module,actor_kind,actor_user_id) VALUES ('bad','Test','Test'," + values + ")", Token));
    }

    [Fact]
    public void Actor_attribution_uses_authenticated_server_claims_and_rejects_invalid_shapes()
    {
        var unauthenticated = new ClaimsPrincipal(new ClaimsIdentity(new[]
            { new Claim(ClaimTypes.NameIdentifier, "7"), new Claim(ClaimTypes.Name, "Forged") }));
        Assert.Equal(LogActor.Anonymous, LogActor.FromPrincipal(unauthenticated));
        var authenticated = new ClaimsPrincipal(new ClaimsIdentity(new[]
            { new Claim(ClaimTypes.NameIdentifier, "7"), new Claim(ClaimTypes.Name, "Aadil") }, "TestServer"));
        Assert.Equal(new LogActor("authenticated", 7, "Aadil"), LogActor.FromPrincipal(authenticated));
        Assert.Throws<ArgumentException>(() => Sanitizer.ValidateActor(new("system", 7, "System")));
        Assert.Throws<ArgumentException>(() => Sanitizer.ValidateActor(new("authenticated", null, "Admin")));
    }

    private static async Task ChangeSourceAsync(PostgresTransactionContext context, int row, CancellationToken token)
    {
        await using var command = context.CreateCommand("UPDATE public.client_data SET row_data='{\"changed\":true}'::jsonb WHERE id=$1");
        command.Parameters.AddWithValue(row);
        await command.ExecuteNonQueryAsync(token);
    }

    private async Task<bool> SourceChangedAsync(int row, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand("SELECT row_data ? 'changed' FROM public.client_data WHERE id=$1", connection);
        command.Parameters.AddWithValue(row);
        return (bool)(await command.ExecuteScalarAsync(token))!;
    }

    private async Task<long> CountEventAsync(Guid id, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand("SELECT count(*) FROM public.audit_log WHERE event_id=$1", connection);
        command.Parameters.AddWithValue(id);
        return (long)(await command.ExecuteScalarAsync(token))!;
    }

    private async Task ControlAsync(string sql, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(token);
    }
}
