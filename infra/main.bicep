// Brainstem on Azure Container Apps.
//
// Deliberately Container Apps rather than AKS: the point of the Azure track is to
// prove the OTel -> Azure Monitor path and run the real topology in the cloud, and
// ACA does that in ~3 minutes with a scale-to-zero cost profile. AKS is a
// documented next step, not a pretence.
//
// Two phases, controlled by deployApps, because the container apps cannot be
// created until their images exist in the registry:
//   1. deployApps=false  -> Log Analytics, App Insights, ACR, ACA environment
//   2. az acr build ...  -> images built server-side (no local Docker needed)
//   3. deployApps=true   -> the collector and the topology
//
// scripts/deploy-azure.ps1 runs all three.

targetScope = 'resourceGroup'

@description('Short name used as a prefix for every resource.')
param namePrefix string = 'brainstem'

@description('Azure region. eastus2 has the most reliable Container Apps quota.')
param location string = resourceGroup().location

@description('False for the first pass (infra only), true once images are pushed.')
param deployApps bool = false

@description('Container image tag to deploy.')
param imageTag string = 'v1'

@description('Log retention. 30 days is the free-tier allowance.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Topology deployed to Container Apps: mirrors topology/small.yaml.')
param topology array = [
  {
    name: 'gateway'
    baseLatencyMs: '4.0'
    latencySigma: '0.35'
    downstream: ['orders', 'recommender']
    emitServerSpans: true
    external: true
    runtime: 'dotnet'
  }
  {
    name: 'orders'
    baseLatencyMs: '6.0'
    latencySigma: '0.40'
    downstream: ['payments', 'inventory']
    emitServerSpans: true
    external: false
    runtime: 'dotnet'
  }
  {
    name: 'payments'
    baseLatencyMs: '8.0'
    latencySigma: '0.45'
    downstream: ['ledger']
    emitServerSpans: true
    external: false
    runtime: 'dotnet'
  }
  {
    name: 'inventory'
    baseLatencyMs: '5.0'
    latencySigma: '0.35'
    downstream: ['cache']
    emitServerSpans: true
    external: false
    runtime: 'dotnet'
  }
  {
    name: 'ledger'
    baseLatencyMs: '9.0'
    latencySigma: '0.50'
    downstream: []
    emitServerSpans: false
    external: false
    runtime: 'dotnet'
  }
  {
    name: 'cache'
    baseLatencyMs: '1.2'
    latencySigma: '0.30'
    downstream: []
    emitServerSpans: false
    external: false
    runtime: 'dotnet'
  }
  {
    name: 'recommender'
    baseLatencyMs: '22.0'
    latencySigma: '0.55'
    downstream: ['inventory']
    emitServerSpans: true
    external: false
    runtime: 'python'
  }
]

var uniq = uniqueString(resourceGroup().id)
var acrName = toLower('${namePrefix}acr${uniq}')

// --- observability -----------------------------------------------------------

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-law'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionInDays
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    // Workspace-based: classic App Insights is retired.
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// --- registry ----------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    // Admin user keeps the deploy script to one step. A managed identity with
    // AcrPull is the better production answer and is noted in the README.
    adminUserEnabled: true
  }
}

// --- container apps environment ---------------------------------------------

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// Internal FQDNs are only knowable once the environment exists.
var domain = env.properties.defaultDomain

// --- collector ---------------------------------------------------------------
// The whole collector config travels in an env var: Container Apps has no
// convenient file mount, and the collector supports --config=env:NAME.

var collectorConfig = '''
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch:
    timeout: 5s
    send_batch_size: 1024
  resource:
    attributes:
      - key: deployment.environment
        value: azure-container-apps
        action: upsert
exporters:
  azuremonitor:
    connection_string: ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}
service:
  telemetry:
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      processors: [resource, batch]
      exporters: [azuremonitor]
'''

resource collector 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-otel-collector'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 4317
        transport: 'http2'      // OTLP/gRPC
        allowInsecure: true
      }
      secrets: [
        {
          name: 'appinsights-connection-string'
          value: appInsights.properties.ConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'collector'
          image: 'otel/opentelemetry-collector-contrib:0.109.0'
          args: ['--config=env:OTEL_CONFIG']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'OTEL_CONFIG', value: collectorConfig }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'appinsights-connection-string'
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// Container Apps ingress listens on 80/443 and forwards to targetPort, so callers
// address the FQDN on port 80 -- NOT :4317. Pointing at :4317 reaches nothing and
// the exporter fails silently while the app keeps serving traffic normally.
//
// The port is stated explicitly because the Python OTLP *gRPC* exporter defaults
// to 4317 when the endpoint carries no port, which silently drops that service's
// traces. The .NET exporter infers 80 either way.
var collectorEndpoint = 'http://${namePrefix}-otel-collector.internal.${domain}:80'

// --- topology ----------------------------------------------------------------

resource apps 'Microsoft.App/containerApps@2024-03-01' = [for svc in topology: if (deployApps) {
  name: '${namePrefix}-${svc.name}'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: bool(svc.external)
        targetPort: 8080
        transport: 'auto'
        allowInsecure: true
      }
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
      ]
    }
    template: {
      containers: [
        {
          name: svc.name
          image: '${acr.properties.loginServer}/brainstem-${svc.runtime}:${imageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'SERVICE_NAME', value: svc.name }
            { name: 'BASE_LATENCY_MS', value: svc.baseLatencyMs }
            { name: 'LATENCY_SIGMA', value: svc.latencySigma }
            { name: 'BASE_ERROR_RATE', value: '0.002' }
            { name: 'ERROR_PROPAGATION', value: '0.85' }
            { name: 'EMIT_SERVER_SPANS', value: string(svc.emitServerSpans) }
            { name: 'OTEL_EXPORTER_OTLP_ENDPOINT', value: collectorEndpoint }
            { name: 'OTEL_EXPORTER_OTLP_PROTOCOL', value: 'grpc' }
            {
              name: 'DOWNSTREAM'
              value: join(map(svc.downstream, d =>
                '${d}=http://${namePrefix}-${d}.internal.${domain}'), ',')
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [collector]
}]

// --- outputs -----------------------------------------------------------------

output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output environmentName string = env.name
output appInsightsName string = appInsights.name
output logAnalyticsWorkspace string = law.name
// Safe dereference: apps[] does not exist on the infra-only (deployApps=false) pass.
output gatewayFqdn string = apps[0].?properties.configuration.ingress.fqdn ?? ''
output collectorEndpoint string = collectorEndpoint
