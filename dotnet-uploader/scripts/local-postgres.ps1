[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Initialize', 'Start', 'Stop', 'Status', 'UseLocal', 'UseSupabase')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$port = 55432
$runtimeRole = 'audit_platform_runtime'
$database = 'audit_platform_local'
$adminRole = 'audit_platform_local_admin'
$ownerMarker = 'AuditPlatform local development PostgreSQL; managed by scripts/local-postgres.ps1'
$uploaderRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$toolsRoot = Join-Path $uploaderRoot '.tools'
$localRoot = Join-Path $toolsRoot 'local-postgres'
$dataDirectory = Join-Path $localRoot 'data'
$markerPath = Join-Path $localRoot 'owner.txt'
$connectionPath = Join-Path $localRoot 'runtime-connection.txt'
$binaryDirectory = Join-Path $toolsRoot 'postgresql-17.11\pgsql\bin'
$dotnet = Join-Path $uploaderRoot '.dotnet\dotnet.exe'
$project = Join-Path $uploaderRoot 'src\AuditPlatform.Api\AuditPlatform.Api.csproj'

function Get-PostgresTool([string]$name) {
    $path = Join-Path $binaryDirectory ($name + '.exe')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "The local PostgreSQL tool is missing: $path"
    }
    return $path
}

function Invoke-Tool([string]$path, [string[]]$arguments) {
    & $path @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL operation failed ($([IO.Path]::GetFileName($path)), exit $LASTEXITCODE). Existing files were retained."
    }
}

function Protect-LocalDirectory([string]$path) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($identity.User)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($identity.User, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance, $propagation, $allow)
        $security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $path -AclObject $security
}

function Assert-OwnedCluster {
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or
        (Get-Content -LiteralPath $markerPath -Raw).Trim() -ne $ownerMarker -or
        -not (Test-Path -LiteralPath (Join-Path $dataDirectory 'PG_VERSION') -PathType Leaf)) {
        throw 'This is not a complete local PostgreSQL cluster created by this script; no files were changed.'
    }
    foreach ($path in @($localRoot, $dataDirectory)) {
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The local PostgreSQL directory cannot be a junction or symbolic link.'
        }
    }
}

function Set-UserSecret([string]$key, [string]$value) {
    if (-not (Test-Path -LiteralPath $dotnet -PathType Leaf)) { throw 'The repository .NET SDK host is missing.' }
    Invoke-Tool $dotnet @('user-secrets', 'set', $key, $value, '--project', $project)
}

function Get-ServerStatus {
    Assert-OwnedCluster
    $ctl = Get-PostgresTool 'pg_ctl'
    & $ctl '-D' $dataDirectory 'status' *> $null
    return ($LASTEXITCODE -eq 0)
}

switch ($Action) {
    'Initialize' {
        if (Test-Path -LiteralPath $localRoot) {
            throw 'The local database folder already exists. It was left untouched; inspect it before deciding what to do.'
        }
        foreach ($tool in @('initdb', 'pg_ctl', 'psql')) { $null = Get-PostgresTool $tool }
        if (-not (Test-Path -LiteralPath $dotnet -PathType Leaf) -or -not (Test-Path -LiteralPath $project -PathType Leaf)) {
            throw 'The repository .NET SDK host or API project is missing.'
        }

        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
        try { $listener.Start() } catch { throw "Local PostgreSQL port $port is already in use; no database files were created." }
        finally { $listener.Stop() }

        $null = New-Item -ItemType Directory -Path $localRoot
        Protect-LocalDirectory $localRoot
        Set-Content -LiteralPath $markerPath -Value $ownerMarker -Encoding Ascii
        $null = New-Item -ItemType Directory -Path $dataDirectory
        $adminPasswordFile = Join-Path $localRoot 'init-password.tmp'
        $sqlPath = Join-Path $localRoot 'create-runtime.tmp.sql'
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $adminBytes = New-Object byte[] 32
            $runtimeBytes = New-Object byte[] 32
            $random.GetBytes($adminBytes)
            $random.GetBytes($runtimeBytes)
            $adminPassword = [BitConverter]::ToString($adminBytes).Replace('-', '').ToLowerInvariant()
            $runtimePassword = [BitConverter]::ToString($runtimeBytes).Replace('-', '').ToLowerInvariant()
            [IO.File]::WriteAllText($adminPasswordFile, $adminPassword, [Text.Encoding]::ASCII)

            Invoke-Tool (Get-PostgresTool 'initdb') @('-D', $dataDirectory, '-U', $adminRole, '--auth=scram-sha-256', "--pwfile=$adminPasswordFile", '--encoding=UTF8', '--locale=C')
            [IO.File]::AppendAllText((Join-Path $dataDirectory 'postgresql.conf'), "`nlisten_addresses = '127.0.0.1'`nport = $port`nmax_connections = 30`nshared_buffers = '32MB'`nssl = off`n")
            Invoke-Tool (Get-PostgresTool 'pg_ctl') @('-D', $dataDirectory, '-l', (Join-Path $localRoot 'postgres.log'), '-w', '-t', '30', 'start')

            [IO.File]::WriteAllText($sqlPath, "CREATE ROLE $runtimeRole LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$runtimePassword';`nCREATE DATABASE $database OWNER $runtimeRole;`n", [Text.Encoding]::ASCII)
            $previousPassword = $env:PGPASSWORD
            try {
                $env:PGPASSWORD = $adminPassword
                Invoke-Tool (Get-PostgresTool 'psql') @('--host=127.0.0.1', "--port=$port", "--username=$adminRole", '--dbname=postgres', '--set=ON_ERROR_STOP=1', "--file=$sqlPath")
            }
            finally { $env:PGPASSWORD = $previousPassword }

            $connection = "Host=127.0.0.1;Port=$port;Database=$database;Username=$runtimeRole;Password=$runtimePassword;SSL Mode=Disable;Timeout=5;Command Timeout=30;Include Error Detail=false;Log Parameters=false"
            [IO.File]::WriteAllText($connectionPath, $connection, [Text.Encoding]::ASCII)
            Set-UserSecret 'Postgres:LocalConnectionString' $connection
            Write-Output "Created a private, loopback-only PostgreSQL cluster on port $port. C# runtime connection is stored in User Secrets."
            Write-Output 'The database is empty. Do not select it in the app until the approved schema/data snapshot is restored.'
        }
        finally {
            $random.Dispose()
            Remove-Item -LiteralPath $adminPasswordFile, $sqlPath -Force -ErrorAction SilentlyContinue
            if ($null -ne $adminBytes) { [Array]::Clear($adminBytes, 0, $adminBytes.Length) }
            if ($null -ne $runtimeBytes) { [Array]::Clear($runtimeBytes, 0, $runtimeBytes.Length) }
        }
    }
    'Start' {
        Assert-OwnedCluster
        if (Get-ServerStatus) { Write-Output "Local PostgreSQL is already running on port $port."; break }
        Invoke-Tool (Get-PostgresTool 'pg_ctl') @('-D', $dataDirectory, '-l', (Join-Path $localRoot 'postgres.log'), '-w', '-t', '30', 'start')
        Write-Output "Started the owned local PostgreSQL cluster on port $port."
    }
    'Stop' {
        Assert-OwnedCluster
        if (-not (Get-ServerStatus)) { Write-Output 'Local PostgreSQL is already stopped.'; break }
        Invoke-Tool (Get-PostgresTool 'pg_ctl') @('-D', $dataDirectory, '-m', 'fast', '-w', '-t', '30', 'stop')
        Write-Output 'Stopped the owned local PostgreSQL cluster.'
    }
    'Status' {
        Assert-OwnedCluster
        if (Get-ServerStatus) { Write-Output "Local PostgreSQL is running on port $port." }
        else { Write-Output 'Local PostgreSQL is stopped.' }
    }
    'UseLocal' {
        if (-not (Get-ServerStatus)) { throw 'Start the owned local PostgreSQL cluster before selecting it for C#.' }
        if (-not (Test-Path -LiteralPath $connectionPath -PathType Leaf)) { throw 'The local runtime connection is missing; initialize the cluster first.' }
        Set-UserSecret 'Postgres:LocalConnectionString' (Get-Content -LiteralPath $connectionPath -Raw)
        Set-UserSecret 'Postgres:Profile' 'Local'
        Set-UserSecret 'Postgres:Enabled' 'true'
        Write-Output 'C# now selects the local database. Supabase credentials were not changed.'
    }
    'UseSupabase' {
        Set-UserSecret 'Postgres:Profile' 'Supabase'
        Write-Output 'C# now selects its existing Supabase connection. Supabase credentials were not changed.'
    }
}
