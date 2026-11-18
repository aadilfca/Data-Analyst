FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /workspace

COPY dotnet-uploader/global.json dotnet-uploader/global.json
COPY dotnet-uploader/src/AuditPlatform.Api/ dotnet-uploader/src/AuditPlatform.Api/
COPY dotnet-uploader/frontend/ dotnet-uploader/frontend/
COPY static/ static/

WORKDIR /workspace/dotnet-uploader
RUN dotnet restore src/AuditPlatform.Api/AuditPlatform.Api.csproj -p:NuGetAudit=false
RUN dotnet publish src/AuditPlatform.Api/AuditPlatform.Api.csproj \
    --configuration Release --no-restore --output /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS runtime
WORKDIR /app
COPY --from=build /app/publish/ ./
USER $APP_UID
EXPOSE 10000
ENTRYPOINT ["/bin/sh", "-c", "exec dotnet AuditPlatform.Api.dll --urls http://0.0.0.0:${PORT:-10000}"]
