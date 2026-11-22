using System.Net;
using Microsoft.AspNetCore.Mvc.Testing;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class StandaloneRuntimeTests
{
    [Fact]
    public async Task Host_serves_health_and_returns_not_found_for_unmapped_api_routes()
    {
        await using var app = new WebApplicationFactory<Program>();
        using var client = app.CreateClient();

        using var health = await client.GetAsync("/healthz", TestContext.Current.CancellationToken);
        Assert.Equal(HttpStatusCode.OK, health.StatusCode);

        using var unmapped = await client.GetAsync("/api/standalone-unmapped-probe", TestContext.Current.CancellationToken);
        Assert.Equal(HttpStatusCode.NotFound, unmapped.StatusCode);
    }
}
