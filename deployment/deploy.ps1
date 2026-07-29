# ================================================
# deploy.ps1 - Windows Deployment Script for DailyGram Instagram Poster
# Place this file in the "deployment" folder
# ================================================

Write-Host "=== DailyGram Instagram Auto-Poster - Windows Deploy ===" -ForegroundColor Cyan
Write-Host "Current date: \$(Get-Date)`n" -ForegroundColor Gray

# ------------------ Configuration ------------------
\$ProjectRoot = Split-Path -Parent \$PSScriptRoot
\$LambdaFolder = Join-Path \$ProjectRoot "lambda"
\$ZipFile      = Join-Path \$ProjectRoot "lambda.zip"
\$TemplateFile = Join-Path \$ProjectRoot "infra\template.yaml"

# ------------------ Step 1: Create lambda.zip ------------------
Write-Host "Step 1: Creating lambda.zip ..." -ForegroundColor Yellow

if (Test-Path \$ZipFile) {
    Remove-Item \$ZipFile -Force
    Write-Host "   Removed old lambda.zip" -ForegroundColor Gray
}

Set-Location \$LambdaFolder
Compress-Archive -Path * -DestinationPath \$ZipFile -Force
Set-Location \$ProjectRoot

Write-Host "   lambda.zip created successfully (\$((Get-Item \$ZipFile).Length / 1KB) KB)`n" -ForegroundColor Green

# ------------------ Step 2: SAM Build ------------------
Write-Host "Step 2: SAM Build ..." -ForegroundColor Yellow
sam build --template \$TemplateFile

if (\$LASTEXITCODE -ne 0) {
    Write-Host "SAM Build failed! Please check the error above." -ForegroundColor Red
    exit 1
}
Write-Host "   SAM Build completed successfully`n" -ForegroundColor Green

# ------------------ Step 3: SAM Deploy ------------------
Write-Host "Step 3: SAM Deploy ..." -ForegroundColor Yellow
Write-Host "This may take 1-2 minutes. Please wait..." -ForegroundColor Gray

sam deploy --no-confirm-changeset

if (\$LASTEXITCODE -ne 0) {
    Write-Host "`nDeployment failed. Please check the error messages above." -ForegroundColor Red
    Write-Host "Common fixes:" -ForegroundColor Yellow
    Write-Host "  • Run 'sam --version' to check if SAM CLI is installed" -ForegroundColor Gray
    Write-Host "  • Make sure you have AWS credentials configured (aws configure)" -ForegroundColor Gray
} else {
    Write-Host "`n✅ Deployment completed successfully!" -ForegroundColor Green
    Write-Host "Your Lambda function should now be updating." -ForegroundColor Green
}

Write-Host "`nPress any key to exit..." -ForegroundColor Gray
\$null = \$Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")