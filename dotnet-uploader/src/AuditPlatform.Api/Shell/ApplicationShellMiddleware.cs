using AuditPlatform.Api.Authentication;
using AuditPlatform.Api.Configuration;
using AuditPlatform.Api.Tenancy;
using Microsoft.Extensions.Options;

namespace AuditPlatform.Api.Shell;

// Only copied, flat public assets are served. No repository, workbook or
// upload directory is mounted. HTML access is rechecked before its bytes leave.
public sealed class ApplicationShellMiddleware(RequestDelegate next)
{
    private static readonly HashSet<string> PublicPages = ["login.html","app_info.html","privacy.html","terms.html"];
    private static readonly HashSet<string> AdminPages = ["users.html","clients.html","setup.html","storage_cleanup.html","error_dashboard.html"];
    // App Settings remains reachable by non-admins, and the retired settings
    // bookmark is informational. Neither requires a selected client.
    private static readonly HashSet<string> NoClientPages = [..AdminPages,"settings.html","client_settings.html","client_select.html"];

    public async Task InvokeAsync(HttpContext context, IOptions<ApplicationShellOptions> options,
        TenantSessionService tenants, AuditPlatform.Api.Clients.TenantClientService clients,
        IOptions<SettingsManagementOptions> settings,IOptions<ColumnManagementOptions> columns,IOptions<TemplateManagementOptions> templates,IOptions<SavedMappingOptions> mappings,
        IOptions<ConfigurationBackupOptions> backups,IOptions<UploadUiOptions> uploadUi,IOptions<UploadStagingOptions> staging)
    {
        if (!options.Value.Enabled) { await next(context); return; }
        var path=context.Request.Path.Value ?? "/";
        if (path=="/" || path.StartsWith("/static/",StringComparison.Ordinal) || path.StartsWith("/shell/",StringComparison.Ordinal))
            context.Response.Headers.CacheControl="no-store";
        var session=context.Items[typeof(VerifiedSession)] as VerifiedSession;
        // An assertion prevents an old tab's form from acting in a different
        // selected tenant. The services repeat the version check inside writes.
        var canonicalApi=path.TrimEnd('/').ToLowerInvariant();
        var boundApi=context.Request.Path.StartsWithSegments("/api/users")
            || context.Request.Path.StartsWithSegments("/api/clients")
            || context.Request.Path.StartsWithSegments("/api/settings")
            || context.Request.Path.StartsWithSegments("/api/columns")
            || context.Request.Path.StartsWithSegments("/api/templates")
            || context.Request.Path.StartsWithSegments("/api/backup")
            || context.Request.Path.StartsWithSegments("/api/upload/save-mapping")
            || context.Request.Path.StartsWithSegments("/api/upload/saved-mapping")
            || context.Request.Path.StartsWithSegments("/api/upload/file")
            || context.Request.Path.StartsWithSegments("/api/upload/staging")
            || canonicalApi=="/api/import-excel"
            || canonicalApi is "/api/auth/current-client" or "/api/auth/select-client";
        if (boundApi && session is not null && (session.TenantId is null
            || context.Request.Headers["X-App-Context"].Count!=1
            || context.Request.Headers["X-App-Context"]!=TenantSessionService.ContextKey(session)))
        {
            context.Response.StatusCode=409; context.Response.Headers["X-Context-Changed"]="true";
            context.Response.Headers.CacheControl="no-store"; return;
        }
        if (path=="/")
        {
            context.Response.Headers.CacheControl="no-store";
            context.Response.Redirect(session is null ? "/static/login.html" : "/static/client_select.html"); return;
        }
        if (path=="/static/account_control_center.html")
        {
            var setup=context.Request.Query["setup"].ToString().Trim().ToLowerInvariant();
            var tab=context.Request.Query["tab"].ToString().Trim().ToLowerInvariant();
            static string? Workflow(string value) => value switch {
                "financial" or "notes"=>"financial", "cashflow" or "treasury" or "template"=>"cashflow",
                "verification"=>"verification", _=>null };
            var target=new Dictionary<string,string?> { ["view"]="available",["workflow"]=Workflow(setup)??(tab is "financial" or "notes" or "cashflow" or "verification" ? Workflow(tab):null)??"verification" };
            if (Workflow(setup) is not null) target["setup"]=setup;
            var upload=context.Request.Query["upload_id"].ToString().Trim();
            if (long.TryParse(upload,out var uploadId) && uploadId>0) target["upload_id"]=upload;
            context.Response.Headers.CacheControl="no-store";
            context.Response.Redirect("/static/trial_balance.html"+QueryString.Create(target)); return;
        }
        var shell=path.StartsWith("/shell/",StringComparison.Ordinal);
        if (!shell && !path.StartsWith("/static/",StringComparison.Ordinal)) { await next(context); return; }
        if (!HttpMethods.IsGet(context.Request.Method) && !HttpMethods.IsHead(context.Request.Method))
        { context.Response.StatusCode=405; return; }
        var file=path[(shell ? 7 : 8)..];
        var extension=Path.GetExtension(file);
        if (file.Length==0 || file[0]=='.' || file.Any(c=>!char.IsAsciiLetterOrDigit(c) && c is not ('_' or '-' or '.'))
            || extension is not (".html" or ".js" or ".css" or ".svg" or ".png"))
        { context.Response.StatusCode=404; return; }
        var fullPath=Path.Combine(AppContext.BaseDirectory,shell ? "ShellFrontend" : "ShellStatic",file);
        if (!File.Exists(fullPath)) { context.Response.StatusCode=404; return; }
        var html=extension==".html";
        var picker=shell && file=="tenant_select.html";
        TenantSessionResult? tenantContext=null;
        if (html && !PublicPages.Contains(file))
        {
            if (session is null) { context.Response.Redirect("/static/login.html"); return; }
            tenantContext=await tenants.ReadAsync(session,context.RequestAborted);
            if (!tenantContext.Authenticated) { context.Response.Redirect("/static/login.html"); return; }
            if (!picker && tenantContext.SelectedTenantId is null) { context.Response.Redirect("/shell/tenant_select.html"); return; }
            var current=tenantContext.Tenants.SingleOrDefault(t=>t.Id==tenantContext.SelectedTenantId);
            if (AdminPages.Contains(file) && current?.Role!="admin") { context.Response.StatusCode=403; return; }
            if (!picker && !NoClientPages.Contains(file))
            {
                var selected=await clients.SelectionAsync(session,null,context.RequestAborted);
                if (selected.Status!=AuditPlatform.Api.Clients.ClientStatus.Success || selected.Clients?.Count!=1)
                { context.Response.Redirect("/static/client_select.html"); return; }
            }
        }
        context.Response.Headers.CacheControl="no-store";
        context.Response.ContentType=extension switch { ".html"=>"text/html; charset=utf-8", ".js"=>"text/javascript; charset=utf-8",
            ".css"=>"text/css; charset=utf-8", ".svg"=>"image/svg+xml", _=>"image/png" };
        if (HttpMethods.IsHead(context.Request.Method)) return;
        if (!html) { await context.Response.SendFileAsync(fullPath,context.RequestAborted); return; }
        var content=await File.ReadAllTextAsync(fullPath,context.RequestAborted);
        var key=session is null ? "" : TenantSessionService.ContextKey(session);
        var selectedTenant=tenantContext is null ? session?.TenantId : tenantContext.SelectedTenantId;
        var settingsEnabled=settings.Value.Enabled ? "true" : "false";
        var settingsAdmin=tenantContext?.Tenants.SingleOrDefault(t=>t.Id==selectedTenant)?.Role=="admin" ? "true" : "false";
        var columnsEnabled=columns.Value.Enabled ? "true" : "false";
        var templatesEnabled=templates.Value.Enabled ? "true" : "false";
        var mappingsEnabled=mappings.Value.Enabled ? "true" : "false";
        var backupsEnabled=backups.Value.Enabled ? "true" : "false";
        // Numeric/hash-only attributes need no user-controlled HTML. This runs
        // before retained scripts, clearing old browser state before they read it.
        var script=$"<script src=\"/shell/context.js\" data-context=\"{key}\" data-tenant=\"{selectedTenant}\" data-settings-enabled=\"{settingsEnabled}\" data-settings-admin=\"{settingsAdmin}\" data-columns-enabled=\"{columnsEnabled}\" data-templates-enabled=\"{templatesEnabled}\" data-mappings-enabled=\"{mappingsEnabled}\" data-backups-enabled=\"{backupsEnabled}\"></script>";
        script=script.Replace(" data-backups-enabled=",$" data-upload-enabled=\"{(uploadUi.Value.Enabled?"true":"false")}\" data-upload-limit=\"{staging.Value.MaximumFileBytes}\" data-backups-enabled=",StringComparison.Ordinal);
        if(!shell && file=="upload.html")
        {
            // Exact packaged assets only. No nested repository or upload path is served.
            var coordinator=new System.Text.RegularExpressions.Regex("<script src=\"/static/upload\\.js(?:\\?[^\"]*)?\"></script>",
                System.Text.RegularExpressions.RegexOptions.CultureInvariant);
            if(coordinator.Matches(content).Count!=1)
            {
                // A future shared-markup change must not reactivate legacy upload calls.
                context.Response.StatusCode=503;await context.Response.WriteAsync("The native Upload page requires integration review.",context.RequestAborted);return;
            }
            content=coordinator.Replace(content,"<script src=\"/static/api_utils.js\"></script><script src=\"/shell/upload_native.js\"></script>");
            content=content.Replace("/static/vendor/xlsx.full.min.js", "/static/xlsx.full.min.js",StringComparison.Ordinal)
                .Replace("</head>","<link rel=\"stylesheet\" href=\"/shell/upload_native.css\"></head>",StringComparison.Ordinal);
        }
        content=content.Replace("<head>","<head>"+script,StringComparison.Ordinal);
        await context.Response.WriteAsync(content,context.RequestAborted);
    }
}
