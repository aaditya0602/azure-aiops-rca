// One generic service, run once per node in the topology. Behaviour comes
// entirely from environment variables, so the same image is the gateway, an
// order service, or a stand-in datastore.
//
// Two things here mirror the synthetic model deliberately, because the analyzer
// must not be able to tell the difference:
//
//   EMIT_SERVER_SPANS=false  suppresses this service's own server spans, so it is
//                            visible only through its caller's client span. That
//                            is how Postgres and Redis appear in real traces, and
//                            it is the case where self-time localization has
//                            nothing to work with.
//
//   Fault injection lives behind /admin/fault rather than in the load generator,
//   so the fault is inside the process being measured, and the injector can
//   record exactly when it started and stopped.

using System.Diagnostics;
using System.Globalization;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

var builder = WebApplication.CreateBuilder(args);

string serviceName = Env("SERVICE_NAME", "unnamed");
double baseLatencyMs = EnvDouble("BASE_LATENCY_MS", 5.0);
double latencySigma = EnvDouble("LATENCY_SIGMA", 0.4);
double baseErrorRate = EnvDouble("BASE_ERROR_RATE", 0.002);
double errorPropagation = EnvDouble("ERROR_PROPAGATION", 0.85);
bool emitServerSpans = Env("EMIT_SERVER_SPANS", "true")
    .Equals("true", StringComparison.OrdinalIgnoreCase);

// "orders=http://orders:8080,inventory=http://inventory:8080"
var downstream = Env("DOWNSTREAM", "")
    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
    .Select(p => p.Split('=', 2))
    .Where(p => p.Length == 2)
    .Select(p => (Name: p[0], Url: p[1].TrimEnd('/')))
    .ToArray();

builder.Services.AddHttpClient("downstream", c =>
{
    c.Timeout = TimeSpan.FromSeconds(10);
});

builder.Services.AddOpenTelemetry()
    .ConfigureResource(r => r.AddService(serviceName))
    .WithTracing(t =>
    {
        t.AddAspNetCoreInstrumentation(o =>
        {
            // Health and admin traffic is not part of the workload under study,
            // and an uninstrumented node emits no server spans at all.
            o.Filter = ctx =>
                emitServerSpans
                && !ctx.Request.Path.StartsWithSegments("/healthz")
                && !ctx.Request.Path.StartsWithSegments("/admin");
        });
        t.AddHttpClientInstrumentation(o =>
        {
            // The analyzer derives the call graph from peer.service on client
            // spans, so the logical topology name has to be on the span. Without
            // this it would only see host:port and could not name a node.
            o.EnrichWithHttpRequestMessage = (activity, req) =>
            {
                var peer = req.Options.TryGetValue(
                    new HttpRequestOptionsKey<string>("peer.service"), out var p) ? p : null;
                if (!string.IsNullOrEmpty(peer))
                {
                    activity.SetTag("peer.service", peer);
                }
            };
        });
        t.AddOtlpExporter();
    });

var app = builder.Build();
var faults = new FaultState();
var rng = new ThreadLocalRandom(seed: serviceName.GetHashCode());

app.MapGet("/healthz", () => Results.Ok(new { service = serviceName, ok = true }));

app.MapGet("/admin/fault", () => Results.Ok(faults.Snapshot()));

// Body: {"kind":"latency","magnitude":6.0,"ttlSeconds":30}
app.MapPost("/admin/fault", (FaultRequest req) =>
{
    faults.Set(req.Kind, req.Magnitude, req.TtlSeconds);
    return Results.Ok(faults.Snapshot());
});

app.MapDelete("/admin/fault", () =>
{
    faults.Clear();
    return Results.Ok(faults.Snapshot());
});

app.MapGet("/work", async (IHttpClientFactory factory, CancellationToken ct) =>
{
    var fault = faults.Active();

    // Own work. dep_fail returns fast, which is exactly why latency-based
    // localization misses it.
    double own = LogNormal(rng, baseLatencyMs, latencySigma);
    if (fault is not null)
    {
        double ramp = fault.Ramp();
        switch (fault.Kind)
        {
            case "latency":
            case "memleak":
                own *= 1.0 + (fault.Magnitude - 1.0) * ramp;
                break;
            case "cpu":
                own *= 1.0 + (fault.Magnitude - 1.0) * ramp;
                own *= LogNormal(rng, 1.0, 0.6);
                break;
            case "dep_fail":
                own *= 1.0 - 0.65 * Math.Clamp(fault.Magnitude / 0.95, 0, 1) * ramp;
                break;
        }
    }

    double ownErrorRate = baseErrorRate;
    if (fault is not null && (fault.Kind == "error" || fault.Kind == "dep_fail"))
    {
        ownErrorRate = Math.Max(ownErrorRate, fault.Magnitude * fault.Ramp());
    }

    await Task.Delay(TimeSpan.FromMilliseconds(Math.Max(own, 0.05)), ct);

    if (rng.NextDouble() < ownErrorRate)
    {
        Activity.Current?.SetStatus(ActivityStatusCode.Error, $"{serviceName} own failure");
        return Results.StatusCode(500);
    }

    var client = factory.CreateClient("downstream");
    foreach (var (name, url) in downstream)
    {
        using var msg = new HttpRequestMessage(HttpMethod.Get, $"{url}/work");
        msg.Options.Set(new HttpRequestOptionsKey<string>("peer.service"), name);
        try
        {
            using var resp = await client.SendAsync(msg, ct);
            if (!resp.IsSuccessStatusCode && rng.NextDouble() < errorPropagation)
            {
                Activity.Current?.SetStatus(ActivityStatusCode.Error, $"downstream {name} failed");
                return Results.StatusCode(503);
            }
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            if (rng.NextDouble() < errorPropagation)
            {
                Activity.Current?.SetStatus(ActivityStatusCode.Error, $"downstream {name} unreachable");
                return Results.StatusCode(503);
            }
        }
    }

    return Results.Ok(new { service = serviceName });
});

app.Run();


static string Env(string key, string fallback) =>
    Environment.GetEnvironmentVariable(key) is { Length: > 0 } v ? v : fallback;

static double EnvDouble(string key, double fallback) =>
    double.TryParse(Environment.GetEnvironmentVariable(key),
        NumberStyles.Float, CultureInfo.InvariantCulture, out var v) ? v : fallback;

static double LogNormal(ThreadLocalRandom rng, double median, double sigma)
{
    // Box-Muller, then exponentiate: median * e^(sigma*z).
    double u1 = 1.0 - rng.NextDouble();
    double u2 = rng.NextDouble();
    double z = Math.Sqrt(-2.0 * Math.Log(u1)) * Math.Sin(2.0 * Math.PI * u2);
    return median * Math.Exp(sigma * z);
}


record FaultRequest(string Kind, double Magnitude, double TtlSeconds);

sealed class ActiveFault
{
    public required string Kind { get; init; }
    public required double Magnitude { get; init; }
    public required DateTimeOffset Start { get; init; }
    public required DateTimeOffset End { get; init; }

    // memleak degrades gradually; everything else is abrupt.
    public double Ramp()
    {
        if (Kind != "memleak") return 1.0;
        double total = (End - Start).TotalMilliseconds;
        if (total <= 0) return 1.0;
        double elapsed = (DateTimeOffset.UtcNow - Start).TotalMilliseconds;
        return Math.Clamp(elapsed / total, 0.0, 1.0);
    }
}

sealed class FaultState
{
    private readonly object _lock = new();
    private ActiveFault? _fault;

    public void Set(string kind, double magnitude, double ttlSeconds)
    {
        var now = DateTimeOffset.UtcNow;
        lock (_lock)
        {
            _fault = new ActiveFault
            {
                Kind = kind,
                Magnitude = magnitude,
                Start = now,
                End = now.AddSeconds(ttlSeconds <= 0 ? 30 : ttlSeconds),
            };
        }
    }

    public void Clear()
    {
        lock (_lock) { _fault = null; }
    }

    public ActiveFault? Active()
    {
        lock (_lock)
        {
            if (_fault is null) return null;
            if (DateTimeOffset.UtcNow >= _fault.End) { _fault = null; return null; }
            return _fault;
        }
    }

    public object Snapshot()
    {
        var f = Active();
        return f is null
            ? new { active = false }
            : new { active = true, kind = f.Kind, magnitude = f.Magnitude,
                    endsAt = f.End.ToString("O") };
    }
}

/// <summary>Per-thread RNG. Random is not thread-safe and every request is concurrent.</summary>
sealed class ThreadLocalRandom
{
    private readonly ThreadLocal<Random> _rng;

    public ThreadLocalRandom(int seed)
    {
        int counter = 0;
        _rng = new ThreadLocal<Random>(() =>
            new Random(seed ^ Interlocked.Increment(ref counter) * 7919));
    }

    public double NextDouble() => _rng.Value!.NextDouble();
}
