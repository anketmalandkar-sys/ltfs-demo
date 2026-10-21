# Starts the agent and the dashboard. The image is rebuilt when .env or the keystore password changed: the build
# puts the secrets from .env into an encrypted keystore in the image (your .env stays as it is).
# The running agent keeps working until the new image replaces it, so changing the password is safe.
# The evals image is rebuilt too, so `docker compose run --rm evals` always has the same secrets.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force output | Out-Null

$placeholder = 'CHANGE_ME'
if (-not (Test-Path keystore_password)) { [IO.File]::WriteAllText("$PWD\keystore_password", $placeholder) }
$password = (Get-Content -Raw keystore_password) -replace '[\r\n]', ''
if ($password -eq $placeholder -or $password.Length -lt 8) {
    Write-Host "Set your keystore password first: open .\keystore_password, replace $placeholder with a password"
    Write-Host "of at least 8 characters, save it, and run .\start.ps1 again. (The file is never committed.)"
    exit 1
}

$both = [Text.Encoding]::UTF8.GetBytes((Get-FileHash .env).Hash + (Get-FileHash keystore_password).Hash)
$env:CONFIG_HASH = (Get-FileHash -InputStream ([IO.MemoryStream]::new($both))).Hash.Substring(0, 16)
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker compose --profile evals build evals
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Running. Dashboard: http://localhost:3000   Logs: docker compose logs -f email-agent'
