@echo off
setlocal
set "PROJECT_DIR=%~dp0"
if not defined ASPNETCORE_ENVIRONMENT set "ASPNETCORE_ENVIRONMENT=Development"
if not defined VerificationRuntime__Enabled set "VerificationRuntime__Enabled=true"
set "TRAY_EXE=%PROJECT_DIR%tools\AuditPlatform.Tray\bin\Release\net10.0-windows\AuditPlatform.Tray.exe"
set "API_DLL=%PROJECT_DIR%src\AuditPlatform.Api\bin\Release\net10.0\AuditPlatform.Api.dll"
if exist "%PROJECT_DIR%.dotnet\dotnet.exe" set "DOTNET_HOST_PATH=%PROJECT_DIR%.dotnet\dotnet.exe"
if not exist "%TRAY_EXE%" (
  echo C# tray application is not built yet. Build AuditPlatform.slnx in Release configuration.
  pause
  exit /b 1
)
if not exist "%API_DLL%" (
  echo C# API is not built yet. Build AuditPlatform.slnx in Release configuration.
  pause
  exit /b 1
)
set "AUDIT_PLATFORM_API_DLL=%API_DLL%"
start "" "%TRAY_EXE%"
