using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Coexistence;
using AuditPlatform.Api.Data;
using AuditPlatform.Api.Health;
using AuditPlatform.Api.Logging;
using AuditPlatform.Api.Middleware;
using AuditPlatform.Api.Tenancy;
using AuditPlatform.Api.Users;
using AuditPlatform.Api.Clients;
using AuditPlatform.Api.Shell;
using AuditPlatform.Api.Settings;
using AuditPlatform.Api.Columns;
using AuditPlatform.Api.Templates;
using AuditPlatform.Api.Mappings;
using AuditPlatform.Api.ConfigurationBackup;
using AuditPlatform.Api.Uploads;
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
builder.Services.AddOptions<UserManagementOptions>()
    .Bind(builder.Configuration.GetSection(UserManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<PostgresSessionStore>();
builder.Services.AddSingleton<AuthenticationService>();
// Internal foundation only; no public route or database migration is enabled.
builder.Services.AddSingleton<TenantContextResolver>();
builder.Services.AddSingleton<EngagementAccessResolver>();
builder.Services.AddSingleton<TenantUserService>();
builder.Services.AddOptions<ClientManagementOptions>()
    .Bind(builder.Configuration.GetSection(ClientManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantClientService>();
builder.Services.AddOptions<ApplicationShellOptions>()
    .Bind(builder.Configuration.GetSection(ApplicationShellOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantSessionService>();
builder.Services.AddOptions<SettingsManagementOptions>()
    .Bind(builder.Configuration.GetSection(SettingsManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantSettingsService>();
builder.Services.AddOptions<ColumnManagementOptions>()
    .Bind(builder.Configuration.GetSection(ColumnManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantColumnService>();
builder.Services.AddOptions<TemplateManagementOptions>()
    .Bind(builder.Configuration.GetSection(TemplateManagementOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantTemplateService>();
builder.Services.AddOptions<SavedMappingOptions>()
    .Bind(builder.Configuration.GetSection(SavedMappingOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<TenantMappingService>();
builder.Services.AddOptions<ConfigurationBackupOptions>()
    .Bind(builder.Configuration.GetSection(ConfigurationBackupOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<ConfigurationBackupService>();
builder.Services.AddOptions<UploadStagingOptions>()
    .Bind(builder.Configuration.GetSection(UploadStagingOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadStagingService>();
builder.Services.AddOptions<UploadPreparationOptions>()
    .Bind(builder.Configuration.GetSection(UploadPreparationOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadPreparationService>();
builder.Services.AddOptions<UploadInputValidationOptions>()
    .Bind(builder.Configuration.GetSection(UploadInputValidationOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadInputValidationService>();
builder.Services.AddOptions<UploadFootingOptions>()
    .Bind(builder.Configuration.GetSection(UploadFootingOptions.SectionName)).ValidateOnStart();
builder.Services.AddSingleton<UploadFootingService>();
builder.Services.AddOptions<UploadCoverageOptions>()
    .Bind(builder.Configuration.GetSection(UploadCoverageOptions.SectionName)).ValidateDataAnnotations().ValidateOnStart();
builder.Services.AddSingleton<UploadCoverageService>();

var app = builder.Build();
if(app.Services.GetRequiredService<IOptions<UploadCoverageOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadFootingOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadCoverageOptions.SectionName,typeof(UploadCoverageOptions),
        ["Coverage requires Footing, input validation and their source-bound tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadFootingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadInputValidationOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadFootingOptions.SectionName,typeof(UploadFootingOptions),
        ["Footing requires input validation and its source-bound tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadInputValidationOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<UploadPreparationOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadInputValidationOptions.SectionName,typeof(UploadInputValidationOptions),
        ["Input validation requires source-bound upload preparation and its tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadPreparationOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<UploadStagingOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled))
    throw new OptionsValidationException(UploadPreparationOptions.SectionName,typeof(UploadPreparationOptions),
        ["Upload preparation requires owned staging, saved mappings and their tenant prerequisites."]);
if(app.Services.GetRequiredService<IOptions<UploadStagingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled)
    throw new OptionsValidationException(UploadStagingOptions.SectionName,typeof(UploadStagingOptions),["Upload staging requires native templates and tenant context."]);
if (app.Services.GetRequiredService<IOptions<ConfigurationBackupOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<SettingsManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled))
    throw new OptionsValidationException(ConfigurationBackupOptions.SectionName,typeof(ConfigurationBackupOptions),
        ["Configuration backup requires tenant settings, saved mappings and their prerequisites."]);
if (app.Services.GetRequiredService<IOptions<SavedMappingOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled)
    throw new OptionsValidationException(SavedMappingOptions.SectionName,typeof(SavedMappingOptions),
        ["Saved mappings require native templates and their tenant context."]);
if (app.Services.GetRequiredService<IOptions<TemplateManagementOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<ColumnManagementOptions>>().Value.Enabled))
    throw new OptionsValidationException(TemplateManagementOptions.SectionName,typeof(TemplateManagementOptions),
        ["Tenant templates require the application shell and tenant columns."]);
if (app.Services.GetRequiredService<IOptions<ColumnManagementOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled)
    throw new OptionsValidationException(ColumnManagementOptions.SectionName,typeof(ColumnManagementOptions),
        ["Tenant columns require the application shell and its session tenant context."]);
if (app.Services.GetRequiredService<IOptions<SettingsManagementOptions>>().Value.Enabled
    && !app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled)
    throw new OptionsValidationException(SettingsManagementOptions.SectionName,typeof(SettingsManagementOptions),
        ["Tenant settings require the application shell and its session tenant context."]);
if (app.Services.GetRequiredService<IOptions<ApplicationShellOptions>>().Value.Enabled
    && (!app.Services.GetRequiredService<IOptions<AuthenticationOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<ClientManagementOptions>>().Value.Enabled
        || !app.Services.GetRequiredService<IOptions<UserManagementOptions>>().Value.Enabled))
    throw new OptionsValidationException(ApplicationShellOptions.SectionName,typeof(ApplicationShellOptions),
        ["Application shell requires authentication, user management and client management."]);
// Invalid/duplicate registrations fail before listening. Production currently
// registers no bridge routes and retains P07's default access block.
_ = app.Services.GetRequiredService<CoexistenceRouteTable>();

app.UseMiddleware<ExceptionLoggingMiddleware>();
app.UseRouting();
app.UseMiddleware<SessionAuthenticationHandler>();
app.UseWhen(context => context.RequestServices.GetRequiredService<IOptions<AuthenticationOptions>>().Value.Enabled
    && (context.Request.Path.StartsWithSegments("/api/auth")
    || context.Request.Path.StartsWithSegments("/api/users")
    || context.Request.Path.StartsWithSegments("/api/clients")
    || context.Request.Path.StartsWithSegments("/api/settings")
    || context.Request.Path.StartsWithSegments("/api/columns")
    || context.Request.Path.StartsWithSegments("/api/templates")
    || context.Request.Path.StartsWithSegments("/api/backup")
    || context.Request.Path.StartsWithSegments("/api/import-excel")
    || context.Request.Path.StartsWithSegments("/api/upload/staging")
    || context.Request.Path.StartsWithSegments("/api/upload/file")
    || context.Request.Path.StartsWithSegments("/api/upload/save-mapping")
    || context.Request.Path.StartsWithSegments("/api/upload/saved-mapping")
    || (context.RequestServices.GetRequiredService<IOptions<CoexistenceOptions>>().Value.Enabled
        && context.RequestServices.GetRequiredService<CoexistenceRouteTable>()
            .Find(context.Request.Method, context.Request.Path.Value ?? "") is not null)),
    branch => branch.UseMiddleware<CsrfMiddleware>());
app.UseMiddleware<ApplicationShellMiddleware>();
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
