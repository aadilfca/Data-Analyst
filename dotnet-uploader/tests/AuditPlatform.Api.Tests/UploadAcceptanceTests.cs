using System.Net;
using AuditPlatform.Api.Configuration;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Xunit;

namespace AuditPlatform.Api.Tests;

public sealed class UploadAcceptanceTests
{
    [Fact]
    public async Task DisabledUiRequiresJobsAndShell()
    {
        await using var app = new WebApplicationFactory<Program>();
        Assert.False(app.Services.GetRequiredService<IOptions<UploadUiOptions>>().Value.Enabled);
        await using var invalid = new WebApplicationFactory<Program>().WithWebHostBuilder(b => b.ConfigureServices(s =>
            s.PostConfigure<UploadUiOptions>(o => o.Enabled = true)));
        Assert.Throws<OptionsValidationException>(() => invalid.CreateClient());
    }

    [Fact]
    public async Task NativeUploadAssetsAreServedWithFixedSyntheticFixture()
    {
        string directory = NativeCompatibilityFixtureFactory.CreateWorkspace("p29", "p29-native-upload");
        using var oracle = NativeCompatibilityFixtureFactory.ReadOracle(directory);
        Assert.True(oracle.RootElement.TryGetProperty("original", out _));

        await using var app = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.ConfigureServices(services =>
            {
                services.PostConfigure<AuthenticationOptions>(options =>
                {
                    options.Enabled = true;
                    options.PublicOrigin = "http://127.0.0.1:5081";
                });
                services.PostConfigure<ApplicationShellOptions>(options => options.Enabled = true);
                services.PostConfigure<UserManagementOptions>(options => options.Enabled = true);
                services.PostConfigure<ClientManagementOptions>(options => options.Enabled = true);
            });
        });
        using var client = app.CreateClient(new WebApplicationFactoryClientOptions { AllowAutoRedirect = false });
        foreach (var asset in new[] { "/static/upload_native.js", "/static/setup.js" })
        {
            using var response = await client.GetAsync(asset, TestContext.Current.CancellationToken);
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            Assert.Equal("text/javascript", response.Content.Headers.ContentType?.MediaType);
            Assert.NotEmpty(await response.Content.ReadAsStringAsync(TestContext.Current.CancellationToken));
        }
    }
}
