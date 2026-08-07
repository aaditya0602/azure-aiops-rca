<#
.SYNOPSIS
  Deploy Brainstem to Azure Container Apps.

.DESCRIPTION
  Three phases, because container apps cannot reference images that do not exist:
    1. Bicep with deployApps=false  -> Log Analytics, App Insights, ACR, ACA env
    2. az acr build                 -> images built IN AZURE (no local Docker)
    3. Bicep with deployApps=true   -> collector + topology

  Idempotent: re-running redeploys in place. Tear down with scripts/destroy-azure.ps1,
  which is the thing to run when you are done, because a running environment bills
  even while idle.

.EXAMPLE
  ./scripts/deploy-azure.ps1 -SubscriptionId 73a32af3-26e8-408d-bfd5-3aae4451792b
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SubscriptionId,
    [string]$ResourceGroup = 'brainstem-rg',
    [string]$Location = 'eastus2',
    [string]$NamePrefix = 'brainstem',
    [string]$ImageTag = 'v1'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

Step 0 "Selecting subscription $SubscriptionId"
az account set --subscription $SubscriptionId
az account show --query '{name:name, id:id}' -o table

Step 1 "Registering resource providers (idempotent, silent when already done)"
foreach ($ns in @('Microsoft.App', 'Microsoft.OperationalInsights', 'Microsoft.Insights',
                  'Microsoft.ContainerRegistry')) {
    az provider register --namespace $ns --wait | Out-Null
    Write-Host "  $ns ok"
}

Step 2 "Creating resource group $ResourceGroup in $Location"
az group create --name $ResourceGroup --location $Location -o none

Step 3 "Deploying infrastructure (Log Analytics, App Insights, ACR, ACA env)"
$infra = az deployment group create `
    --resource-group $ResourceGroup `
    --name "brainstem-infra" `
    --template-file "$repo/infra/main.bicep" `
    --parameters namePrefix=$NamePrefix location=$Location deployApps=false `
    --query properties.outputs -o json | ConvertFrom-Json

$acrName = $infra.acrName.value
$acrServer = $infra.acrLoginServer.value
Write-Host "  registry: $acrServer"
Write-Host "  app insights: $($infra.appInsightsName.value)"

Step 4 "Building images in ACR (server-side; local Docker not required)"
az acr build --registry $acrName --image "brainstem-dotnet:$ImageTag" `
    "$repo/services/node" -o none
Write-Host "  brainstem-dotnet:$ImageTag built"
az acr build --registry $acrName --image "brainstem-python:$ImageTag" `
    "$repo/services/recommender" -o none
Write-Host "  brainstem-python:$ImageTag built"

Step 5 "Deploying collector and topology"
$apps = az deployment group create `
    --resource-group $ResourceGroup `
    --name "brainstem-apps" `
    --template-file "$repo/infra/main.bicep" `
    --parameters namePrefix=$NamePrefix location=$Location deployApps=true `
                 imageTag=$ImageTag `
    --query properties.outputs -o json | ConvertFrom-Json

$fqdn = $apps.gatewayFqdn.value
Write-Host "`nDeployed." -ForegroundColor Green
Write-Host "  gateway:      https://$fqdn/work"
Write-Host "  healthz:      https://$fqdn/healthz"
Write-Host "  app insights: $($infra.appInsightsName.value) (resource group $ResourceGroup)"
Write-Host @"

Next:
  # drive traffic and inject faults against the deployed stack
  .\.venv\Scripts\python.exe harness\loadgen.py --url https://$fqdn/work --rps 10 --duration 600

  # confirm spans reached Application Insights
  az monitor app-insights query --app $($infra.appInsightsName.value) -g $ResourceGroup ``
    --analytics-query "dependencies | summarize count() by target | order by count_ desc"

  # tear down when finished (an idle environment still bills)
  .\scripts\destroy-azure.ps1 -SubscriptionId $SubscriptionId -ResourceGroup $ResourceGroup
"@
