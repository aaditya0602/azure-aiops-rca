<#
.SYNOPSIS
  Deploy Brainstem to Azure Container Apps.

.DESCRIPTION
  Three phases, because container apps cannot reference images that do not exist:
    1. Bicep with deployApps=false  -> Log Analytics, App Insights, ACR, ACA env
    2. docker build + push to ACR   -> needs a running Docker daemon
    3. Bicep with deployApps=true   -> collector + topology

  Azure for Students cannot use server-side ACR Tasks (`az acr build` returns
  TasksOperationsNotAllowed), so images are built locally by default. Pass
  -UseAcrTasks on a subscription where Tasks is permitted.

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
    [string]$ImageTag = 'v1',

    # Build images server-side with ACR Tasks instead of locally. Not available on
    # Azure for Students (TasksOperationsNotAllowed); default is a local build.
    [switch]$UseAcrTasks
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

# $ErrorActionPreference does NOT apply to native executables: az can fail and the
# script sails on. An earlier version of this script reported "image built" after
# ACR refused the request, then deployed apps whose images did not exist.
function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "$what failed with exit code $LASTEXITCODE"
    }
}

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

Step 4 "Building and pushing images"
# NOTE: `az acr build` (server-side ACR Tasks) is NOT available on Azure for
# Students - it returns TasksOperationsNotAllowed. The registry itself works
# fine, so images are built locally and pushed. Requires a running Docker daemon.
# On a subscription where ACR Tasks is permitted, -UseAcrTasks skips the local build.
if ($UseAcrTasks) {
    foreach ($pair in @(@('brainstem-dotnet', 'services/node'),
                        @('brainstem-python', 'services/recommender'))) {
        az acr build --registry $acrName --image "$($pair[0]):$ImageTag" `
            "$repo/$($pair[1])" -o none
        Assert-LastExit "az acr build $($pair[0])"
        Write-Host "  $($pair[0]):$ImageTag built in ACR"
    }
} else {
    docker info --format '{{.ServerVersion}}' | Out-Null
    Assert-LastExit "docker daemon check (start Docker Desktop, or pass -UseAcrTasks)"

    az acr login --name $acrName
    Assert-LastExit "az acr login"

    foreach ($pair in @(@('brainstem-dotnet', 'services/node'),
                        @('brainstem-python', 'services/recommender'))) {
        $tag = "$acrServer/$($pair[0]):$ImageTag"
        Write-Host "  building $tag"
        docker build -t $tag "$repo/$($pair[1])"
        Assert-LastExit "docker build $($pair[0])"
        docker push $tag
        Assert-LastExit "docker push $($pair[0])"
        Write-Host "  pushed $tag"
    }
}

# Refuse to deploy apps against images that are not actually in the registry.
$repos = az acr repository list --name $acrName -o json | ConvertFrom-Json
foreach ($needed in @('brainstem-dotnet', 'brainstem-python')) {
    if ($repos -notcontains $needed) {
        throw "image '$needed' is not in registry $acrName - refusing to deploy apps that cannot pull. Repositories present: $($repos -join ', ')"
    }
}
Write-Host "  registry contains: $($repos -join ', ')"

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
