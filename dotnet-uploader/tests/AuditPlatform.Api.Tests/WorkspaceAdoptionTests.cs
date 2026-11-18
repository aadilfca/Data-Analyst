using System.Text.Json;
using Xunit;
using Adoption = AuditPlatform.WorkspaceAdoption.Program;

namespace AuditPlatform.Api.Tests;

public sealed class WorkspaceAdoptionTests
{
    [Theory]
    [InlineData("Host=remote.example;Database=backup_rehearsal_test;Port=5432")]
    [InlineData("Host=127.0.0.1;Database=postgres;Port=5432")]
    [InlineData("Host=127.0.0.1;Database=backup_rehearsal_test;Port=543")]
    public void Remote_and_ordinary_targets_are_rejected_before_connecting(string connection)
        => Assert.Throws<InvalidOperationException>(() => Adoption.ValidateLocalConnection(connection));

    [Fact]
    public void Preview_is_default_and_duplicate_operator_options_are_rejected()
    {
        Assert.False(Adoption.ParseArguments(["--owner-dir", "owned"]).ContainsKey("apply"));
        Assert.Throws<InvalidOperationException>(() => Adoption.ParseArguments(["--owner-dir", "one", "--owner-dir", "two"]));
    }

    [Fact]
    public void Unset_active_role_needs_explicit_exclusion_and_inactive_accounts_stay_inactive()
    {
        using var data = JsonDocument.Parse("""
            [{"id":1,"role":"admin","active":true},
             {"id":2,"role":"admin","active":false},
             {"id":3,"role":null,"active":true},
             {"id":4,"role":"analyst","active":true}]
            """);
        var users = data.RootElement.EnumerateArray().ToArray();
        Assert.Throws<InvalidOperationException>(() => Adoption.ResolveMemberships(users, []));
        var selected = Adoption.ResolveMemberships(users, [3]);
        Assert.Equal(new[] { 1, 2, 4 }, selected.Select(user => user.GetProperty("id").GetInt32()).ToArray());
        Assert.False(selected[1].GetProperty("active").GetBoolean());
        Assert.Equal("analyst", selected[2].GetProperty("role").GetString());
    }

    [Fact]
    public void First_workspace_requires_an_existing_active_administrator()
    {
        using var data = JsonDocument.Parse("""[{"id":1,"role":"analyst","active":true}]""");
        Assert.Throws<InvalidOperationException>(() => Adoption.ResolveMemberships(data.RootElement.EnumerateArray().ToArray(), []));
    }
}
