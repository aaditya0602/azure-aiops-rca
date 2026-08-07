<#
.SYNOPSIS
  Delete the Brainstem resource group and everything in it.

.DESCRIPTION
  Run this when you are done. A Container Apps environment plus Log Analytics
  bills while idle, and on a student subscription that credit is finite.

  This is destructive and irreversible: it deletes the resource group, which takes
  the registry, images, workspace and all ingested telemetry with it. It prompts
  before doing so unless -Force is given.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SubscriptionId,
    [string]$ResourceGroup = 'brainstem-rg',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

az account set --subscription $SubscriptionId

Write-Host "Resources that will be deleted from '$ResourceGroup':" -ForegroundColor Yellow
az resource list --resource-group $ResourceGroup --query '[].{name:name, type:type}' -o table

if (-not $Force) {
    $answer = Read-Host "`nDelete resource group '$ResourceGroup' and ALL of the above? (yes/no)"
    if ($answer -ne 'yes') {
        Write-Host "Aborted. Nothing was deleted."
        exit 0
    }
}

az group delete --name $ResourceGroup --yes --no-wait
Write-Host "Deletion started (running in background)." -ForegroundColor Green
Write-Host "Check with: az group show --name $ResourceGroup"
