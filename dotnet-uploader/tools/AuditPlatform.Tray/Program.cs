using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Windows.Forms;

namespace AuditPlatform.Tray;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        using var singleInstance = new Mutex(true, @"Local\AuditPlatformCSharpTray", out var created);
        if (!created) return;

        ApplicationConfiguration.Initialize();
        Application.Run(new TrayApplication());
    }
}

internal sealed class TrayApplication : ApplicationContext
{
    private static readonly Uri BaseUri = new("http://127.0.0.1:5080/");
    private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(1);
    private readonly NotifyIcon _icon;
    private readonly ToolStripMenuItem _status;
    private readonly ToolStripMenuItem _start;
    private readonly ToolStripMenuItem _stop;
    private readonly HttpClient _http = new() { Timeout = HealthTimeout };
    private readonly System.Windows.Forms.Timer _poll = new() { Interval = 2000 };
    private Process? _server;
    private bool _busy;
    private bool _exiting;

    public TrayApplication()
    {
        _status = new ToolStripMenuItem("C# server: checking…") { Enabled = false };
        _start = new ToolStripMenuItem("Start C# server", null, async (_, _) => await StartAsync());
        _stop = new ToolStripMenuItem("Stop C# server", null, async (_, _) => await StopAsync());
        var menu = new ContextMenuStrip();
        menu.Items.Add(_status);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_start);
        menu.Items.Add(_stop);
        menu.Items.Add(new ToolStripMenuItem("Open C# app", null, (_, _) => OpenApp()));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Exit tray", null, (_, _) => ExitThread()));

        _icon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            Text = "Audit Platform — C#",
            ContextMenuStrip = menu,
            Visible = true
        };
        _icon.DoubleClick += (_, _) => OpenApp();
        _poll.Tick += async (_, _) => await RefreshAsync();
        _poll.Start();
        _ = RefreshAsync();
    }

    private async Task StartAsync()
    {
        if (_busy) return;
        var existingIssue = await ReadinessIssueAsync();
        if (existingIssue is null) { UpdateMenu(true, "C# server: running"); return; }
        if (existingIssue != "server_not_responding")
        {
            UpdateMenu(false, $"C# server: not ready ({existingIssue})");
            ShowReadinessFailure(existingIssue);
            return;
        }
        _busy = true;
        UpdateMenu(false, "C# server: starting…");
        try
        {
            var apiPath = Environment.GetEnvironmentVariable("AUDIT_PLATFORM_API_DLL");
            if (string.IsNullOrWhiteSpace(apiPath)) apiPath = Path.Combine(AppContext.BaseDirectory, "AuditPlatform.Api.dll");
            apiPath = Path.GetFullPath(apiPath);
            if (!File.Exists(apiPath))
            {
                MessageBox.Show($"C# API file was not found:\n{apiPath}\n\nSet AUDIT_PLATFORM_API_DLL or place AuditPlatform.Api.dll beside the tray app.",
                    "Audit Platform C#", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            var dotnet = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
            if (string.IsNullOrWhiteSpace(dotnet)) dotnet = "dotnet";
            var start = new ProcessStartInfo(dotnet)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(apiPath)!
            };
            start.ArgumentList.Add(apiPath);
            start.ArgumentList.Add("--urls");
            start.ArgumentList.Add(BaseUri.ToString().TrimEnd('/'));
            start.Environment["ASPNETCORE_ENVIRONMENT"] = Environment.GetEnvironmentVariable("ASPNETCORE_ENVIRONMENT") ?? "Production";
            start.Environment["AUDIT_PLATFORM_TRAY_LOG"] = Path.Combine(Path.GetTempPath(), "audit-platform-csharp.log");
            var logPath = start.Environment["AUDIT_PLATFORM_TRAY_LOG"]!;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            _server = new Process { StartInfo = start, EnableRaisingEvents = true };
            _server.OutputDataReceived += (_, e) => AppendLog(logPath, e.Data);
            _server.ErrorDataReceived += (_, e) => AppendLog(logPath, e.Data);
            _server.Exited += (_, _) => BeginInvokeIfOpen(() => _ = RefreshAsync());
            if (!_server.Start()) throw new InvalidOperationException();
            _server.BeginOutputReadLine();
            _server.BeginErrorReadLine();

            var deadline = DateTimeOffset.UtcNow.AddSeconds(60);
            string? readinessIssue = "server_not_responding";
            while (DateTimeOffset.UtcNow < deadline && !_server.HasExited)
            {
                readinessIssue = await ReadinessIssueAsync();
                if (readinessIssue is null) { UpdateMenu(true, "C# server: running"); return; }
                if (readinessIssue == "database_not_configured") break;
                await Task.Delay(500);
            }
            if (!_server.HasExited) StopOwnedProcess();
            ShowReadinessFailure(readinessIssue ?? "server_not_responding");
        }
        catch
        {
            MessageBox.Show("The C# server could not be started. Check the local .NET installation and operator configuration.",
                "Audit Platform C#", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            _busy = false;
            await RefreshAsync();
        }
    }

    private async Task StopAsync()
    {
        if (_busy || _server is null || _server.HasExited) return;
        _busy = true;
        UpdateMenu(false, "C# server: stopping…");
        try
        {
            StopOwnedProcess();
            var deadline = DateTimeOffset.UtcNow.AddSeconds(15);
            while (DateTimeOffset.UtcNow < deadline && !_server.HasExited) await Task.Delay(250);
            if (!_server.HasExited) _server.Kill(entireProcessTree: true);
        }
        catch { MessageBox.Show("The tray could not stop its own C# server process.", "Audit Platform C#", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        finally { _busy = false; await RefreshAsync(); }
    }

    private void StopOwnedProcess()
    {
        if (_server is { HasExited: false }) _server.Kill(entireProcessTree: true);
    }

    private async Task<string?> ReadinessIssueAsync()
    {
        try
        {
            using var response = await _http.GetAsync(new Uri(BaseUri, "health/app-ready"));
            if (response.StatusCode == HttpStatusCode.OK)
            {
                using var app = await _http.GetAsync(BaseUri);
                return app.IsSuccessStatusCode && app.Content.Headers.ContentType?.MediaType == "text/html"
                    ? null : "application_shell_unavailable";
            }

            var body = await response.Content.ReadAsStringAsync();
            try
            {
                using var document = JsonDocument.Parse(body);
                if (document.RootElement.TryGetProperty("reason", out var reason)
                    && reason.ValueKind == JsonValueKind.String)
                    return reason.GetString() ?? $"http_{(int)response.StatusCode}";
            }
            catch (JsonException) { }

            return $"http_{(int)response.StatusCode}";
        }
        catch (TaskCanceledException) { return "health_check_timeout"; }
        catch (HttpRequestException) { return "server_not_responding"; }
    }

    private static void ShowReadinessFailure(string issue)
    {
        var guidance = issue switch
        {
            "database_not_configured" => "Configure a dedicated non-production PostgreSQL database. See dotnet-uploader/OPERATIONS.md.",
            "application_shell_not_configured" => "Enable ApplicationShell in local C# settings. See dotnet-uploader/OPERATIONS.md.",
            "authentication_not_configured" => "Enable Authentication and set its local public origin. See dotnet-uploader/OPERATIONS.md.",
            "user_management_not_configured" => "Enable UserManagement in local C# settings. See dotnet-uploader/OPERATIONS.md.",
            "client_management_not_configured" => "Enable ClientManagement in local C# settings. See dotnet-uploader/OPERATIONS.md.",
            "application_shell_unavailable" => "Check the packaged C# frontend files and the startup log at:\n" + Path.Combine(Path.GetTempPath(), "audit-platform-csharp.log"),
            _ => "Check PostgreSQL and the startup log at:\n" + Path.Combine(Path.GetTempPath(), "audit-platform-csharp.log")
        };
        MessageBox.Show($"The C# API is not ready ({issue}).\n\n{guidance}",
            "Audit Platform C#", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private async Task RefreshAsync()
    {
        if (_busy) return;
        var issue = await ReadinessIssueAsync();
        var healthy = issue is null;
        var status = healthy ? "C# server: running"
            : issue == "server_not_responding" ? "C# server: stopped" : $"C# server: not ready ({issue})";
        UpdateMenu(healthy, status);
    }

    private void UpdateMenu(bool healthy, string text)
    {
        if (_exiting) return;
        _status.Text = text;
        _start.Enabled = !_busy && !healthy;
        _stop.Enabled = !_busy && _server is { HasExited: false };
        _icon.Text = healthy ? "Audit Platform C# — running" : "Audit Platform C# — stopped";
    }

    private static void OpenApp()
    {
        try { Process.Start(new ProcessStartInfo(BaseUri.ToString()) { UseShellExecute = true }); }
        catch { MessageBox.Show("The C# app URL could not be opened.", "Audit Platform C#", MessageBoxButtons.OK, MessageBoxIcon.Error); }
    }

    private static void AppendLog(string path, string? line)
    {
        if (line is null) return;
        try { File.AppendAllText(path, line + Environment.NewLine); } catch { }
    }

    private void BeginInvokeIfOpen(Action action)
    {
        if (_exiting) return;
        try { _icon.ContextMenuStrip?.BeginInvoke(action); } catch (InvalidOperationException) { }
    }

    protected override void ExitThreadCore()
    {
        _exiting = true;
        _poll.Stop();
        _icon.Visible = false;
        _icon.Dispose();
        _http.Dispose();
        // Exiting the tray never stops a running server.
        base.ExitThreadCore();
    }
}
