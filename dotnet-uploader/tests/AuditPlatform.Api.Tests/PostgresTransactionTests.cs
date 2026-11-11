using System.Net;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Data;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Npgsql;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class PostgresTransactionTests(PostgresFixture fixture) : IClassFixture<PostgresFixture>
{
    private static CancellationToken TestCancellation => TestContext.Current.CancellationToken;

    [Fact]
    public async Task Successful_nested_work_uses_one_transaction_and_commits_all_rows()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        var observed = await Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 1, token);
            var first = await ScalarAsync<long>(context, "SELECT txid_current()", token);
            // Nested business work receives the same context, not another lease.
            await InsertAsync(context, table, 2, token);
            var second = await ScalarAsync<long>(context, "SELECT txid_current()", token);
            return (first, second);
        }, TestCancellation);
        Assert.Equal(observed.first, observed.second);
        Assert.Equal(2L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Failure_after_first_write_rolls_back_all_rows_without_replay()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        var calls = 0;
        var error = await Assert.ThrowsAsync<PostgresException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await InsertAsync(context, table, 1, token);
            await InsertAsync(context, table, 1, token);
            return true;
        }, TestCancellation));
        Assert.Equal(PostgresErrorCodes.UniqueViolation, error.SqlState);
        Assert.Equal(1, calls);
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Swallowed_sql_failure_cannot_report_a_successful_commit()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool();
        var error = await Assert.ThrowsAsync<PostgresException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 1, token);
            await Assert.ThrowsAsync<PostgresException>(() => InsertAsync(context, table, 1, token));
            return "must not escape";
        }, TestCancellation));
        Assert.Equal(PostgresErrorCodes.InFailedSqlTransaction, error.SqlState);
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Explicit_savepoint_recovery_preserves_the_outer_transaction()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool();
        await Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 1, token);
            await context.Transaction.SaveAsync("optional_check", token);
            await Assert.ThrowsAsync<PostgresException>(() => InsertAsync(context, table, 1, token));
            await context.Transaction.RollbackAsync("optional_check", token);
            await context.Transaction.ReleaseAsync("optional_check", token);
            await InsertAsync(context, table, 2, token);
            return true;
        }, TestCancellation);
        Assert.Equal(2L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Cancellation_after_a_write_rolls_back_and_releases_capacity()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(TestCancellation);
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 1, token);
            await cancellation.CancelAsync();
            return true;
        }, cancellation.Token));
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Connection_loss_before_commit_rolls_back_without_retry()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool();
        var calls = 0;
        await Assert.ThrowsAnyAsync<NpgsqlException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await InsertAsync(context, table, 1, token);
            await TerminateAsync(context.Connection.ProcessID, token);
            return await ScalarAsync<int>(context, "SELECT 1", token);
        }, TestCancellation));
        Assert.Equal(1, calls);
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Lost_commit_acknowledgement_reports_unknown_after_actual_commit_and_never_replays()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var proxy = fixture.CreateCommitProxy();
        await using var pool = fixture.CreatePool(proxy: proxy);
        var calls = 0;
        var error = await Assert.ThrowsAsync<PostgresCommitOutcomeUnknownException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await InsertAsync(context, table, 1, token);
            proxy.Arm();
            return true;
        }, TestCancellation));
        await proxy.AcknowledgementDropped.WaitAsync(TimeSpan.FromSeconds(5), TestCancellation);
        Assert.Equal(1, calls);
        Assert.Contains("Reconcile", error.Message);
        // Independent connection bypasses the relay and proves the server kept
        // the row despite the client never receiving COMMIT completion.
        Assert.Equal(1L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Definite_commit_rejection_preserves_the_server_error_and_rolls_back()
    {
        var table = await fixture.CreateTableAsync(TestCancellation, deferredKey: true);
        await using var pool = fixture.CreatePool();
        var calls = 0;
        var error = await Assert.ThrowsAsync<PostgresException>(() => Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await InsertAsync(context, table, 1, token);
            await InsertAsync(context, table, 1, token);
            return true;
        }, TestCancellation));
        Assert.Equal(PostgresErrorCodes.UniqueViolation, error.SqlState);
        Assert.Equal(1, calls);
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Snapshot_keeps_all_reads_consistent_while_another_user_commits_changes()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await ControlAsync($"INSERT INTO {table} VALUES (1,10)", TestCancellation);
        await using var pool = fixture.CreatePool();
        var snapshot = Snapshot(pool);
        var result = await snapshot.ExecuteAsync(async (context, token) =>
        {
            var first = await ScalarAsync<decimal>(context, $"SELECT sum(amount) FROM {table}", token);
            // The fixture control connection represents a separate user's commit.
            await ControlAsync($"UPDATE {table} SET amount=20; INSERT INTO {table} VALUES (2,30)", token);
            var second = await ScalarAsync<decimal>(context, $"SELECT sum(amount) FROM {table}", token);
            var count = await ScalarAsync<long>(context, $"SELECT count(*) FROM {table}", token);
            return (first, second, count);
        }, TestCancellation);
        Assert.Equal((10m, 10m, 1L), result);
        var fresh = await snapshot.ExecuteAsync((context, token) =>
            ScalarAsync<decimal>(context, $"SELECT sum(amount) FROM {table}", token), TestCancellation);
        Assert.Equal(50m, fresh);
    }

    [Fact]
    public async Task Snapshot_rejects_writes_and_does_not_leak_read_only_state()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        var error = await Assert.ThrowsAsync<PostgresException>(() => Snapshot(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 1, token);
            return true;
        }, TestCancellation));
        Assert.Equal(PostgresErrorCodes.ReadOnlySqlTransaction, error.SqlState);
        Assert.Equal(0L, await CountAsync(table, TestCancellation));
        await Transactions(pool).ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, 2, token);
            return true;
        }, TestCancellation);
        Assert.Equal(1L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Broken_snapshot_restarts_once_on_a_new_connection_and_discards_partial_results()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await ControlAsync($"INSERT INTO {table} VALUES (1,10)", TestCancellation);
        await using var pool = fixture.CreatePool();
        var calls = 0;
        var backends = new List<int>();
        var result = await Snapshot(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            backends.Add(context.Connection.ProcessID);
            var rows = new List<decimal>
            {
                await ScalarAsync<decimal>(context, $"SELECT sum(amount) FROM {table}", token)
            };
            if (calls == 1)
            {
                await TerminateAsync(context.Connection.ProcessID, token);
                await ControlAsync($"UPDATE {table} SET amount=20", token);
                await ScalarAsync<int>(context, "SELECT 1", token);
            }
            return rows;
        }, TestCancellation);
        Assert.Equal(2, calls);
        Assert.Equal(20m, Assert.Single(result));
        Assert.NotEqual(backends[0], backends[1]);
    }

    [Fact]
    public async Task Second_snapshot_disconnect_fails_without_returning_partial_data()
    {
        await using var pool = fixture.CreatePool();
        var calls = 0;
        await Assert.ThrowsAnyAsync<NpgsqlException>(() => Snapshot(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await ScalarAsync<int>(context, "SELECT 1", token);
            await TerminateAsync(context.Connection.ProcessID, token);
            return await ScalarAsync<int>(context, "SELECT 2", token);
        }, TestCancellation));
        Assert.Equal(2, calls);
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Theory]
    [InlineData("SELECT missing_p04b_column", "42703")]
    [InlineData("SELECT 1/0", "22012")]
    public async Task Snapshot_sql_errors_are_not_retried(string sql, string expectedCode)
    {
        await using var pool = fixture.CreatePool();
        var calls = 0;
        var error = await Assert.ThrowsAsync<PostgresException>(() => Snapshot(pool).ExecuteAsync(async (context, token) =>
        {
            calls++;
            return await ScalarAsync<int>(context, sql, token);
        }, TestCancellation));
        Assert.Equal(expectedCode, error.SqlState);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task Snapshot_statement_timeout_is_not_retried_and_capacity_recovers()
    {
        await using var pool = fixture.CreatePool(maximumPoolSize: 1, timeoutSeconds: 2);
        var calls = 0;
        await Assert.ThrowsAnyAsync<NpgsqlException>(() => Snapshot(pool, timeoutSeconds: 1).ExecuteAsync(async (context, token) =>
        {
            calls++;
            return await ScalarAsync<int>(context, "SELECT 1 FROM pg_sleep(10)", token);
        }, TestCancellation));
        Assert.Equal(1, calls);
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Snapshot_cancellation_is_not_retried_and_does_not_leak_session_limits()
    {
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(TestCancellation);
        var calls = 0;
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => Snapshot(pool, timeoutSeconds: 1).ExecuteAsync(async (context, token) =>
        {
            calls++;
            await ScalarAsync<int>(context, "SELECT 1", token);
            await cancellation.CancelAsync();
            return true;
        }, cancellation.Token));
        Assert.Equal(1, calls);
        await using var connection = await pool.DataSource!.OpenConnectionAsync(TestCancellation);
        await using var command = new NpgsqlCommand(
            "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), current_setting('transaction_read_only')", connection);
        await using var reader = await command.ExecuteReaderAsync(TestCancellation);
        Assert.True(await reader.ReadAsync(TestCancellation));
        Assert.Equal("0", reader.GetString(0));
        Assert.Equal("0", reader.GetString(1));
        Assert.Equal("off", reader.GetString(2));
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(false, true)]
    [InlineData(true, false)]
    [InlineData(true, true)]
    public async Task Nested_roots_are_rejected_before_borrowing_another_connection(bool outerRead, bool innerRead)
    {
        await using var pool = fixture.CreatePool(maximumPoolSize: 1);
        var writes = Transactions(pool);
        var reads = Snapshot(pool);
        Task<int> Work(PostgresTransactionContext context, CancellationToken token) => innerRead
            ? reads.ExecuteAsync((inner, ct) => ScalarAsync<int>(inner, "SELECT 1", ct), token)
            : writes.ExecuteAsync((inner, ct) => ScalarAsync<int>(inner, "SELECT 1", ct), token);
        await Assert.ThrowsAsync<InvalidOperationException>(() => outerRead
            ? reads.ExecuteAsync(Work, TestCancellation)
            : writes.ExecuteAsync(Work, TestCancellation));
        await AssertPoolUsableAsync(pool, TestCancellation);
    }

    [Fact]
    public async Task Independent_concurrent_operations_do_not_share_transaction_scope()
    {
        var table = await fixture.CreateTableAsync(TestCancellation);
        await using var pool = fixture.CreatePool(maximumPoolSize: 2);
        var writes = Transactions(pool);
        var arrived = 0;
        var both = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        Task<int> Write(int id) => writes.ExecuteAsync(async (context, token) =>
        {
            await InsertAsync(context, table, id, token);
            if (Interlocked.Increment(ref arrived) == 2)
                both.TrySetResult();
            await both.Task.WaitAsync(TimeSpan.FromSeconds(5), token);
            return context.Connection.ProcessID;
        }, TestCancellation);
        var backends = await Task.WhenAll(Write(1), Write(2));
        Assert.NotEqual(backends[0], backends[1]);
        Assert.Equal(2L, await CountAsync(table, TestCancellation));
    }

    [Fact]
    public async Task Services_are_registered_and_disabled_database_fails_without_running_callbacks()
    {
        await using var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureServices(services => services.PostConfigure<PostgresOptions>(options => options.Enabled = false)));
        using var client = app.CreateClient();
        var writes = app.Services.GetRequiredService<PostgresTransactions>();
        var reads = app.Services.GetRequiredService<PostgresReadSnapshot>();
        Assert.Same(writes, app.Services.GetRequiredService<PostgresTransactions>());
        var calls = 0;
        Task<int> Work(PostgresTransactionContext context, CancellationToken token)
        {
            calls++;
            return Task.FromResult(1);
        }
        await Assert.ThrowsAsync<InvalidOperationException>(() => writes.ExecuteAsync(Work, TestCancellation));
        await Assert.ThrowsAsync<InvalidOperationException>(() => reads.ExecuteAsync(Work, TestCancellation));
        Assert.Equal(0, calls);
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/healthz", TestCancellation)).StatusCode);
    }

    private static PostgresTransactions Transactions(PostgresDataSourceFactory pool) =>
        new(pool, Options.Create(new PostgresOptions { CommandTimeoutSeconds = 3 }));

    private static PostgresReadSnapshot Snapshot(PostgresDataSourceFactory pool, int timeoutSeconds = 3) =>
        new(pool, Options.Create(new PostgresOptions { CommandTimeoutSeconds = timeoutSeconds }));

    private static async Task<T> ScalarAsync<T>(PostgresTransactionContext context, string sql, CancellationToken token)
    {
        await using var command = context.CreateCommand(sql);
        return (T)(await command.ExecuteScalarAsync(token))!;
    }

    private static async Task InsertAsync(PostgresTransactionContext context, string table, int id, CancellationToken token)
    {
        await using var command = context.CreateCommand($"INSERT INTO {table} (id,amount) VALUES ($1,$2)");
        command.Parameters.AddWithValue(id);
        command.Parameters.AddWithValue(10m);
        await command.ExecuteNonQueryAsync(token);
    }

    private async Task<long> CountAsync(string table, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand($"SELECT count(*) FROM {table}", connection);
        return (long)(await command.ExecuteScalarAsync(token))!;
    }

    private async Task ControlAsync(string sql, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(token);
    }

    private async Task TerminateAsync(int backend, CancellationToken token)
    {
        await using var connection = await fixture.OpenControlConnectionAsync(token);
        await using var command = new NpgsqlCommand("SELECT pg_terminate_backend($1)", connection);
        command.Parameters.AddWithValue(backend);
        Assert.True((bool)(await command.ExecuteScalarAsync(token))!);
    }

    private static async Task AssertPoolUsableAsync(PostgresDataSourceFactory pool, CancellationToken token)
    {
        var result = await Transactions(pool).ExecuteAsync((context, ct) => ScalarAsync<int>(context, "SELECT 1", ct), token);
        Assert.Equal(1, result);
    }
}
