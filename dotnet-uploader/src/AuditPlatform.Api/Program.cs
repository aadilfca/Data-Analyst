using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Health;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Middleware;
using Microsoft.Extensions.Options;

var builder = WebApplication.CreateBuilder(args);

// Console-only structured logs work on Windows and container hosts without
// requiring permission to register or write a Windows Event Log source.
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole();

builder.Services.AddControllers();
builder.Services.AddOptions<CoexistenceOptions>()
    .Bind(builder.Configuration.GetSection(CoexistenceOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton(provider => new CoexistenceRouteTable(provider.GetServices<CoexistenceRoute>()));
builder.Services.AddSingleton<ICoexistenceAccessGate, SessionCoexistenceGate>();
builder.Services.AddSingleton(provider => new LegacyForwarder(
    provider.GetRequiredService<IOptions<CoexistenceOptions>>(),
    provider.GetRequiredService<ILogger<LegacyForwarder>>()));

// Default configuration supports appsettings and Platform__... environment
// overrides. Validate at startup so invalid settings cannot reach a request.
builder.Services.AddOptions<PlatformOptions>()
    .Bind(builder.Configuration.GetSection(PlatformOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();

builder.Services.AddOptions<PostgresOptions>()
    .Bind(builder.Configuration.GetSection(PostgresOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton<PostgresDataSourceFactory>();
builder.Services.AddSingleton<PostgresTransactions>();
builder.Services.AddSingleton<PostgresReadSnapshot>();
builder.Services.AddSingleton<PostgresReadinessCheck>();
builder.Services.AddOptions<LogPersistenceOptions>()
    .Bind(builder.Configuration.GetSection(LogPersistenceOptions.SectionName))
    .ValidateDataAnnotations()
    .ValidateOnStart();
builder.Services.AddSingleton<LogSanitizer>();
builder.Services.AddSingleton<PostgresAuditWriter>();
builder.Services.AddSingleton<DiagnosticErrorLogger>();
builder.Services.AddOptions<AuthenticationOptions>()
    .Bind(builder.Configuration.GetSection(AuthenticationOptions.SectionName))
    .ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<PostgresSessionStore>();
builder.Services.AddSingleton<AuthenticationService>();

var app = builder.Build();
// Invalid/duplicate registrations fail before listening. Production currently
// registers no bridge routes and retains P07's default access block.
_ = app.Services.GetRequiredService<CoexistenceRouteTable>();

app.UseMiddleware<ExceptionLoggingMiddleware>();
app.UseRouting();
app.UseMiddleware<SessionAuthenticationHandler>();
app.UseWhen(context => context.RequestServices.GetRequiredService<IOptions<AuthenticationOptions>>().Value.Enabled
    && (context.Request.Path.StartsWithSegments("/api/auth")
    || (context.RequestServices.GetRequiredService<IOptions<CoexistenceOptions>>().Value.Enabled
        && context.RequestServices.GetRequiredService<CoexistenceRouteTable>()
            .Find(context.Request.Method, context.Request.Path.Value ?? "") is not null)),
    branch => branch.UseMiddleware<CsrfMiddleware>());
app.UseMiddleware<CoexistenceMiddleware>();
app.MapControllers();

try
{
    app.Run();
    return 0;
}
catch (OptionsValidationException)
{
    // The host logs the validation details. Return a predictable failure code
    // instead of leaving startup failure to the operating-system crash handler.
    return 1;
}

// Expose the entry point to the in-process ASP.NET Core integration tests.
public partial class Program { }
