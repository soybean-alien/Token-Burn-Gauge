// Token Burn Gauge by soybean_alien
// The eight modules and their rows: the same readings, in the same words, as gauge.html.
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TokenGauge;

static class J
{
    public static JsonNode? At(this JsonNode? n, params string[] path)
    {
        foreach (var k in path)
        {
            if (n is JsonObject o && o.TryGetPropertyValue(k, out var c)) n = c; else return null;
        }
        return n;
    }
    public static double? Num(this JsonNode? n) =>
        n is JsonValue v && v.GetValueKind() == JsonValueKind.Number ? v.GetValue<double>() : null;
    public static string? Str(this JsonNode? n) =>
        n is JsonValue v && v.GetValueKind() == JsonValueKind.String ? v.GetValue<string>() : null;
    public static bool IsFalse(this JsonNode? n) => n is JsonValue v && v.GetValueKind() == JsonValueKind.False;
    public static bool Truthy(this JsonNode? n) => n switch
    {
        null => false,
        JsonValue v => v.GetValueKind() switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.Number => v.GetValue<double>() != 0,
            JsonValueKind.String => v.GetValue<string>().Length > 0,
            _ => false,
        },
        _ => true,
    };
    public static int Count(this JsonNode? n) => n is JsonArray a ? a.Count : 0;
    public static List<double> Nums(this JsonNode? n)
    {
        var l = new List<double>();
        if (n is JsonArray a) foreach (var v in a) l.Add(v.Num() ?? 0);
        return l;
    }
}

record DialView(double? Value, double? ScaleMax, bool Log, double? Redline, string State, string Label);
record Row(string K, string V, bool Low = false, double? Bar = null);

class Source
{
    public required string Url;
    public JsonNode? Data;
    public int Fails;
    public string? Err;
    public string? LastSeq;
    public double LastChange;
    public List<Module> Modules = new();
}

class Btn
{
    public required string Target, Label;
    public bool Resume, Armed, Busy, Hover;
    public double ArmUntil;
    public RectangleF Rect;
}

class Module
{
    public required string Id, Src, Title;
    public required Func<JsonNode?, string> What;
    public required Func<JsonNode, DialView?> Dial;
    public required Func<JsonNode, List<Row>> Rows;
    public Func<JsonNode, string?>? Err;
    public Func<JsonNode, double>? Ring;
    public Func<JsonNode, List<double>>? Spark;
    public Func<JsonNode, string>? Unit;
    public int Digits;
    public string SparkUnit = "", SparkWord = "samples";
    public double SparkFloor = 1;
    public bool Own;
    public string? Family;   // Claude Code or Codex: the title becomes the running model, coloured by tier
    public Func<JsonNode, string?>? Model;   // set for hardware: the title is the part's own name, no tier colour
    public string TitleText = "", EffortText = "";
    public int Tier, EffortTier;
    public List<Btn> Buttons = new();

    // What is on screen now.
    public Source Source = null!;
    public string View = "CONNECTING", Why = "", WhatText = "", NumText = "\u2014", UnitText = "", HiText = "", CapText = "", KillMsg = "";
    public bool Dead = true;
    public double NeedleTarget, NeedlePos, NeedleVel, NeedleAlpha, RingTarget, RingPos;
    public double? BandFrom;
    public List<double> SparkData = new();
    public double SparkTop = 1;
    public List<Row> RowData = new();
    public string? Prev;
    public double IdleSince, FlashAt = -1e9;
    public RectangleF Card, Face;
    public float FaceX, FaceY, FaceScale;
}

static class Catalog
{
    static readonly CultureInfo Us = CultureInfo.InvariantCulture;

    public static string F(double? v, int digits) =>
        v is null || double.IsNaN(v.Value) ? "\u2014" : v.Value.ToString("N" + digits, Us);

    // Fraction of the sweep for a value: log for rates, linear for watts.
    public static double Frac(double? v, double? max, bool log)
    {
        if (v is null || max is null || max == 0) return 0;
        var f = log ? Math.Log10(1 + v.Value) / Math.Log10(1 + max.Value) : v.Value / max.Value;
        return Math.Clamp(f, 0, 1);
    }

    static DialView? View(JsonNode? n, string? state = null, string valueKey = "value", bool? log = null, string? label = null) =>
        n is not JsonObject ? null : new DialView(
            n.At(valueKey).Num(), n.At("scale_max").Num(), log ?? n.At("log").Truthy(), n.At("redline").Num(),
            state ?? n.At("state").Str() ?? "IDLE", label ?? n.At("label").Str() ?? "");

    static DialView? Primary(JsonNode d) => View(d.At("dial", "primary"), d.At("dial", "state").Str());
    static DialView? Second(JsonNode d) => View(d.At("detail", "second_dial"));
    static List<double> SecondSpark(JsonNode d) => d.At("detail", "second_dial", "spark").Nums();
    static double N0(JsonNode? n, string k) => n.At(k).Num() ?? 0;

    public static Dictionary<string, Source> Sources() => new()
    {
        ["c"] = new Source { Url = "metrics-c.json" },   // Claude Code
        ["x"] = new Source { Url = "metrics-x.json" },   // Codex
        ["a"] = new Source { Url = "metrics-a.json" },   // local model
        ["b"] = new Source { Url = "metrics-b.json" },   // this PC: GPU and CPU
    };

    public static List<Module> Modules() => new()
    {
        new Module { Id = "c-s", Family = "Claude Code", Src = "c", Title = "Claude Code", What = _ => "tokens per second, this PC",
            Dial = Primary, Ring = d => Frac(d.At("dial", "ring", "value").Num(), 300000, true),
            SparkUnit = "tokens/s", SparkFloor = 50, Rows = RowsClaudeSecond,
            Buttons = { new Btn { Target = "claude", Label = "KILL CLAUDE CODE IN FLIGHT" } } },
        new Module { Id = "c-m", Family = "Claude Code", Src = "c", Title = "Claude Code", What = _ => "new tokens per minute, this PC",
            Dial = Second, Spark = SecondSpark, SparkUnit = "tokens/min", SparkFloor = 1000, Rows = RowsClaudeMinute },
        new Module { Id = "x-s", Family = "Codex", Src = "x", Title = "Codex", What = _ => "tokens per second, this PC",
            Dial = Second, Spark = SecondSpark, SparkUnit = "tokens/s", SparkFloor = 50, Rows = RowsCodexSecond,
            Buttons = { new Btn { Target = "codex", Label = "KILL CODEX IN FLIGHT" } } },
        new Module { Id = "x-m", Family = "Codex", Src = "x", Title = "Codex", What = _ => "new tokens per minute, this PC",
            Dial = Primary, Ring = d => Frac(d.At("dial", "ring", "value").Num(), 2000, true),
            SparkUnit = "tokens/min", SparkFloor = 1000, Rows = RowsCodexMinute },
        // Token burn, not spend (11/11/2026): the wallet does not move while the plan covers use.
        new Module { Id = "a", Family = "Local model", Src = "a", Title = "Local model", What = _ => "new tokens per minute",
            Dial = Primary, Ring = d => Frac(d.At("dial", "ring", "value").Num(), 60, true),
            SparkUnit = "tokens/min", SparkFloor = 1000, Rows = RowsA,
            Buttons = { new Btn { Target = "model", Label = "PAUSE MODEL (NEW RUNS)" },
                        new Btn { Target = "model-resume", Label = "RESUME MODEL", Resume = true } } },
        new Module { Id = "b", Family = "This PC (GPU)", Model = d => d.At("detail", "gpu").Str(), Src = "b", Title = "This PC (GPU)", What = _ => "local hardware load, watts",
            Dial = Primary,
            Ring = d => d.At("dial", "ring", "value").Num() is double v ? Frac(v, 500, true)
                : Frac(d.At("detail", "gpu_util_pct").Num(), 100, false),
            SparkUnit = "W", SparkFloor = 1, Rows = RowsB,
            Buttons = { new Btn { Target = "local", Label = "STOP LOCAL GENERATION" } } },
        new Module { Id = "cpu", Family = "This PC (CPU)", Model = d => d.At("detail", "cpu", "name").Str(), Src = "b", Title = "This PC (CPU)", What = _ => "local hardware load, watts", Own = true,
            Dial = d => View(d.At("detail", "cpu"), valueKey: "watts", label: "CPU package watts"),
            Err = d => d.At("detail", "cpu", "error").Str(),
            Ring = d => Frac(d.At("detail", "cpu", "util_pct").Num(), 100, false),
            Spark = d => d.At("detail", "cpu", "spark").Nums(),
            SparkUnit = "W", SparkFloor = 1, Rows = RowsCpu },
        new Module { Id = "ttft", Src = "c", Title = "Time to first token",
            What = d => "current model: " + (d.At("detail", "model").Str() is { Length: > 0 } m ? m : "none seen yet"),
            Dial = d =>
            {
                var t = d.At("detail", "ttft");
                return View(t, valueKey: "value_s", log: true,
                    label: t.At("waiting").Truthy() ? "seconds waiting since the prompt was sent"
                        : t.At("value_s").Num() is null ? "no request seen yet" : "seconds to first token, last request");
            },
            Spark = d => d.At("detail", "ttft", "spark").Nums(),
            Digits = 1, SparkUnit = "s", SparkFloor = 5, SparkWord = "waits", Rows = RowsFirstToken },
    };

    static string Ago(double sec) =>
        sec < 90 ? F(sec, 0) + " s ago" : sec < 5400 ? F(sec / 60, 0) + " min ago" : F(sec / 3600, 0) + " h ago";

    // Which model is running: the one doing most of the output in the last 10 minutes, then any others.
    static List<Row> ModelRows(JsonNode? x)
    {
        var h = new List<Row>();
        if (x.At("model").Str() is not { Length: > 0 } main) { h.Add(new("model", "none seen yet")); return h; }
        var a = x.At("model_age_s").Num();
        h.Add(new("model", main + (a is null ? "" : a <= 60 ? ", running now" : ", last used " + Ago(a.Value))));
        if (x.At("models") is JsonArray models)
            foreach (var m in models.Skip(1).Take(2))
                h.Add(new("also, last 10 min", (m.At("model").Str() ?? "")
                    + (m.At("share_pct").Num() is double p ? $", {F(p, 0)}% of output" : "")));
        return h;
    }

    static List<Row> RowsClaudeSecond(JsonNode d)
    {
        var x = d.At("detail"); var t = x.At("tap"); var r = t.At("last_request"); var h = ModelRows(x);
        if (x.At("token_error").Truthy()) h.Add(new("token counts", x.At("token_error").Str() ?? "", true));
        if (t is null) h.Add(new("live tap", "not answering; needle is a log average", true));
        else if (r is null) h.Add(new("live tap", "running, no request seen yet"));
        else
        {
            var sent = N0(r, "input") + N0(r, "cache_read") + N0(r, "cache_write");
            h.Add(new("last prompt", (r.At("input").Num() is null ? "sent, not accepted yet" : F(sent, 0) + " tokens")
                + $", {F(r.At("age_s").Num(), 0)} s ago"));
            h.Add(new("last answer", (r.At("output").Num() is double o ? F(o, 0) + " tokens" : F(r.At("output_est").Num(), 0) + " tokens estimated")
                + (r.At("done_s").Num() is double done ? " in " + F(done, 1) + " s" : ", streaming")));
            h.Add(new("requests in flight / seen", $"{F(t.At("active_requests").Num(), 0)} / {F(t.At("requests_total").Num(), 0)}"));
        }
        var st = x.At("stream");
        if (st is not null) h.Add(new("answer pieces this second",
            $"{F(st.At("writes_per_s").Num(), 0)}, {F(st.At("tokens_per_write").Num(), 2)} tokens each ({(st.At("learned").Truthy() ? "learned from logs" : "first guess")})"));
        h.Add(new("counted from", x.At("token_source").Str() ?? ""));
        return h;
    }

    static List<Row> RowsClaudeMinute(JsonNode d)
    {
        var x = d.At("detail");
        return new()
        {
            new("per minute from logs, in / out", $"{F(x.At("tokens_in_per_min").Num(), 0)} / {F(x.At("tokens_out_per_min").Num(), 0)}"),
            new("cache written / read per minute", $"{F(x.At("cache_write_per_min").Num(), 0)} / {F(x.At("cache_read_per_min").Num(), 0)}"),
            new("last 10 seconds", F(x.At("tokens_last_10s").Num(), 0) + " tokens"),
            new("output since the gauge started", F(x.At("since_start", "output").Num(), 0) + " tokens"),
            new("processes, busy / running", $"{x.At("active_pids").Count()} / {F(x.At("processes").Num(), 0)}"),
            new("counted from", "usage counters in Claude Code's session logs"),
        };
    }

    static List<Row> RowsCodexSecond(JsonNode d)
    {
        var x = d.At("detail"); var h = ModelRows(x);
        if (x.At("token_error").Truthy()) h.Add(new("token counts", x.At("token_error").Str() ?? "", true));
        h.Add(new("last 10 seconds", F(x.At("tokens_last_10s").Num(), 0) + " tokens"));
        h.Add(new("bytes its processes wrote", F(d.At("dial", "ring", "value").Num(), 1) + " KB per second (any writing, not only answers)"));
        if (x.At("newest_row_age_s").Num() is double age) h.Add(new("newest count is", F(age, 0) + " s old"));
        h.Add(new("counted from", x.At("token_source").Str() ?? ""));
        return h;
    }

    static List<Row> RowsCodexMinute(JsonNode d)
    {
        var x = d.At("detail");
        return new()
        {
            new("per minute, in / out", $"{F(x.At("tokens_in_per_min").Num(), 0)} / {F(x.At("tokens_out_per_min").Num(), 0)}"),
            new("cache written / read per minute", $"{F(x.At("cache_write_per_min").Num(), 0)} / {F(x.At("cache_read_per_min").Num(), 0)}"),
            new("processes, busy / running", $"{x.At("active_pids").Count()} / {F(x.At("processes").Num(), 0)}"),
            new("last model", x.At("model").Str() is { Length: > 0 } m ? m : "none seen yet"),
        };
    }

    static List<Row> RowsA(JsonNode d)
    {
        var x = d.At("detail"); var h = new List<Row>();
        h.Add(new("per minute, in / out", $"{F(x.At("tokens_in_per_min").Num(), 0)} / {F(x.At("tokens_out_per_min").Num(), 0)}"));
        h.Add(new("cache read per minute", F(x.At("cache_read_per_min").Num(), 0)));
        h.Add(new("API calls per minute", F(x.At("api_calls_per_min").Num(), 1)));
        if (x.At("last_activity_age_s").Num() is double la) h.Add(new("last activity", Ago(la)));
        if (x.At("export_age_s").Num() is double ea) h.Add(new("counts read", Ago(ea)));
        h.Add(new("counted from", x.At("token_source").Str() ?? ""));
        return h;
    }

    static List<Row> RowsB(JsonNode d)
    {
        var x = d.At("detail"); var f = d.At("dial", "fuel");
        double of = N0(f, "of"), pct = of != 0 ? 100 * N0(f, "value") / of : 0;
        var tps = x.At("tokens_per_s").Num() is double t ? F(t, 1)
            : "not measured" + (x.At("proxy").Str() == "down" ? " (proxy not running)" : "");
        var model = x.At("model").Str();
        return new()
        {
            new("VRAM used", $"{F(f.At("value").Num(), 0)} of {F(f.At("of").Num(), 0)} MiB"),
            new("", "", Bar: pct),
            new("tokens per second", tps),
            new("GPU load", F(x.At("gpu_util_pct").Num(), 0) + " %"),
            new("idle baseline", x.At("calibrating").Truthy() ? "measuring (60 s)" : F(x.At("idle_watts").Num(), 1) + " W"),
            new("model runtime", x.At("runtime").Str() == "up" ? "running" + (string.IsNullOrEmpty(model) ? "" : " \u00B7 " + model) : "not running"),
            new("card", x.At("gpu").Str() ?? ""),
        };
    }

    static List<Row> RowsCpu(JsonNode d)
    {
        var c = d.At("detail", "cpu"); var g = d.At("dial", "primary", "value").Num(); var w = c.At("watts").Num();
        return new()
        {
            new("CPU load", F(c.At("util_pct").Num(), 0) + " %"),
            new("", "", Bar: c.At("util_pct").Num() ?? 0),
            new("CPU and GPU together", w is null || g is null ? "not available" : F(w + g, 0) + " W"),
            new("chip", c.At("name").Str() is { Length: > 0 } n ? n : "not known"),
            new("read from", "Windows Energy Meter, CPU package; the rest of the board is not counted"),
        };
    }

    static List<Row> RowsFirstToken(JsonNode d)
    {
        var x = d.At("detail"); var t = x.At("ttft"); var r = x.At("tap", "last_request"); var st = x.At("stream");
        var h = new List<Row>
        {
            new("Claude Code", t.At("waiting").Truthy() ? "waiting for the first token"
                : t.At("value_s").Num() is not double v ? "no request seen yet"
                : F(v, 1) + " s" + (t.At("age_s").Num() is double a ? ", " + F(a, 0) + " s ago" : "")),
        };
        if (r is not null) h.Add(new("time to first token, tap", r.At("first_token_s").Num() is double ft ? F(ft, 2) + " s" : "waiting"));
        if (st is not null) h.Add(new("quiet before last answer began", st.At("wait_s").Num() is double ws
            ? "estimated " + F(ws, 1) + " s, " + F(st.At("wait_age_s").Num(), 0) + " s ago" : "none seen yet"));
        h.Add(new("last model", x.At("model").Str() is { Length: > 0 } m ? m : "none seen yet"));
        h.Add(new("read from", t.At("basis").Str() ?? ""));
        h.Add(new("Codex", "no first-token signal"));
        h.Add(new("Local model", "waits on its stream hook"));
        return h;
    }
}
