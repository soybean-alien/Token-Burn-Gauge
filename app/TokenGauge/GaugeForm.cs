// Token Burn Gauge by soybean_alien
// A native window for the gauge: reads the collectors' JSON on 127.0.0.1 and draws the eight dials itself.
// No browser engine. Counters and timestamps only, the same as the page.
using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace TokenGauge;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        // Without this the .NET heap sizes itself to this PC's large CPU cache (measured: 41 MB private
        // against 21 MB with it). The setting is only read at start, so the app starts itself once with it.
        if (Environment.GetEnvironmentVariable("DOTNET_GCgen0size") is null && Environment.ProcessPath is { } exe)
        {
            var psi = new ProcessStartInfo(exe) { UseShellExecute = false };
            foreach (var a in args) psi.ArgumentList.Add(a);
            psi.Environment["DOTNET_GCgen0size"] = "0x400000";
            Process.Start(psi);
            return;
        }
        StartCollectors();
        ApplicationConfiguration.Initialize();
        Application.Run(new GaugeForm(args.Length > 0 ? args[0] : "http://127.0.0.1:8830/"));
    }

    // Installed copy only: start.cmd sits beside the exe. If nothing answers on the collectors' port,
    // start them (each collector exits by itself if its port is already taken).
    static void StartCollectors()
    {
        var cmd = Path.Combine(AppContext.BaseDirectory, "start.cmd");
        if (!File.Exists(cmd)) return;
        try
        {
            using var probe = new System.Net.Sockets.TcpClient();
            probe.Connect("127.0.0.1", 8830);
            return;
        }
        catch (System.Net.Sockets.SocketException) { }
        try
        {
            Process.Start(new ProcessStartInfo("cmd.exe", $"/c \"{cmd}\"") { CreateNoWindow = true, UseShellExecute = false });
        }
        catch (System.ComponentModel.Win32Exception) { }
    }
}

sealed class GaugeForm : Form
{
    const float CX = 160, CY = 160, R = 120, Sweep = 240, Start = -120;
    const double RestMs = 60000;

    static readonly Color Bg = Hex(0x0a0a0b), Panel = Hex(0x101114), Line = Hex(0x1a2230), Txt = Hex(0xe8e4d8),
        Dim = Hex(0x6b7484), Redline = Hex(0xff3b30), DeadC = Hex(0x3a4150), Slate = Hex(0x56627a);
    static Color Hex(int rgb) => Color.FromArgb(255, Color.FromArgb(rgb));
    static Color StateColor(string view) => view switch
    {
        "IDLE" => Hex(0x5b6472), "WARM" => Hex(0xffd446), "BURNING" => Hex(0xff8a3d), "REDLINE" => Redline, _ => DeadC,
    };
    // Model tiers: 1 purple (top), 2 orange, 3 yellow, 4 blue (bottom); 5 neon purple, the fixed local model; anything else keeps the text colour.
    static Color TierColor(int tier) => tier switch
    {
        1 => Hex(0xb48cff), 2 => Hex(0xff8a3d), 3 => Hex(0xffd446), 4 => Hex(0x5aa9ff), 5 => Hex(0xd400ff), _ => Txt,
    };
    static Color A(Color c, double a) => Color.FromArgb((int)Math.Clamp(a * 255, 0, 255), c);

    readonly string _base;
    readonly HttpClient _http = new(new SocketsHttpHandler { UseProxy = false });
    readonly Dictionary<string, Source> _sources = Catalog.Sources();
    readonly List<Module> _modules = Catalog.Modules();
    readonly Stopwatch _clock = Stopwatch.StartNew();
    readonly System.Windows.Forms.Timer _anim = new() { Interval = 15 };
    readonly string _mono, _serif;
    readonly Font _fNum, _fState, _fScale;   // dial-face fonts, in the face's own 264 by 226 units
    Font _fBody = null!, _fSmall = null!, _fRows = null!, _fRowsBold = null!, _fH1 = null!, _fH2 = null!, _fBtn = null!;
    float _em;
    double _lastTick;
    RectangleF _header, _footer;

    const string Sub = "Four sources, read separately. A source that stops reporting says so; it never reads as zero.";
    const string Foot = "The kill buttons end work in flight: unsaved output of that reply is lost. The pause button starts no new runs: a turn already running finishes, and the model stays paused until RESUME is pressed. No model calls anywhere in this pipeline. Counters and timestamps only.";

    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    public GaugeForm(string baseUrl)
    {
        _base = baseUrl.EndsWith('/') ? baseUrl : baseUrl + "/";
        Text = "Token-burn gauge";
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        BackColor = Bg;
        StartPosition = FormStartPosition.Manual;
        var wa = Screen.PrimaryScreen!.WorkingArea;                 // half the screen, on the right
        Bounds = new Rectangle(wa.X + wa.Width / 2, wa.Y, wa.Width / 2, wa.Height);
        MinimumSize = new Size(520, 480);
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);

        using (var fc = new InstalledFontCollection())
        {
            var have = fc.Families.Select(f => f.Name).ToHashSet(StringComparer.OrdinalIgnoreCase);
            _mono = new[] { "JetBrains Mono", "Cascadia Mono", "Consolas" }.FirstOrDefault(have.Contains) ?? FontFamily.GenericMonospace.Name;
            _serif = new[] { "Instrument Serif", "Georgia" }.FirstOrDefault(have.Contains) ?? FontFamily.GenericSerif.Name;
        }
        _fNum = new Font(_mono, 46, FontStyle.Bold, GraphicsUnit.Pixel);
        _fState = new Font(_mono, 19, FontStyle.Bold, GraphicsUnit.Pixel);
        _fScale = new Font(_mono, 13, FontStyle.Regular, GraphicsUnit.Pixel);

        var now = _clock.Elapsed.TotalMilliseconds;
        foreach (var m in _modules) { m.Source = _sources[m.Src]; m.Source.Modules.Add(m); m.IdleSince = now; m.WhatText = m.What(null); }
        _anim.Tick += (_, _) => Animate();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        int dark = 1;
        DwmSetWindowAttribute(Handle, 20, ref dark, sizeof(int));   // dark title bar
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        _anim.Start();
        foreach (var s in _sources.Values) _ = Poll(s);
    }

    // ---- reading -------------------------------------------------------------------------------------------

    async Task Poll(Source s)
    {
        while (!IsDisposed)
        {
            try
            {
                using var cts = new CancellationTokenSource(4000);
                using var r = await _http.GetAsync(_base + s.Url, cts.Token);
                var body = await r.Content.ReadAsStringAsync(cts.Token);
                if (!r.IsSuccessStatusCode)
                {
                    var msg = "HTTP " + (int)r.StatusCode;
                    try { msg = JsonNode.Parse(body).At("error").Str() ?? msg; } catch { /* not JSON */ }
                    throw new InvalidOperationException(msg);
                }
                var d = JsonNode.Parse(body);
                var seq = d.At("seq")?.ToJsonString();
                if (seq != s.LastSeq) { s.LastSeq = seq; s.LastChange = _clock.Elapsed.TotalMilliseconds; }
                s.Data = d; s.Fails = 0; s.Err = null;
            }
            catch (OperationCanceledException) { s.Fails++; s.Err = "no answer within 4 s"; }
            catch (Exception e) { s.Fails++; s.Err = e.InnerException?.Message ?? e.Message; }
            if (IsDisposed) return;
            foreach (var m in s.Modules)
            {
                try { Read(m); } catch (Exception e) { m.Why = "could not read this sample: " + e.Message; }
                if (WindowState != FormWindowState.Minimized) Invalidate(Rectangle.Ceiling(m.Card));
            }
            await Task.Delay(TimeSpan.FromSeconds(Math.Clamp(s.Data.At("poll_s").Num() ?? 1, 0.2, 60)));
        }
    }

    // One sample into what the module shows. A dead source says so; it never reads as zero.
    void Read(Module m)
    {
        var s = m.Source; var now = _clock.Elapsed.TotalMilliseconds; var d = s.Data;
        var dl = d is not null && d.At("dial") is not null ? m.Dial(d) : null;
        string view, why = "";
        if (s.Fails >= 3 || (d is null && s.Fails >= 1)) { view = "OFFLINE"; why = s.Err ?? "source unreachable"; }
        else if (d is null) view = "CONNECTING";
        else if (d.At("source_ok").IsFalse() && !m.Own) { view = "OFFLINE"; why = d.At("error").Str() ?? "source reports a failure"; }
        else if (now - s.LastChange > (d.At("stale_after_s").Num() ?? 10) * 1000) { view = "STALE"; why = d.At("error").Str() ?? "no new sample from the collector"; }
        else if (d.At("dial") is not null && dl is null) { view = "OFFLINE"; why = "the collector does not send this reading yet"; }
        else if (m.Err?.Invoke(d) is { Length: > 0 } err) { view = "OFFLINE"; why = err; }
        else view = dl?.State ?? "OFFLINE";

        var live = dl is not null && view == dl.State && (m.Own || !d.At("source_ok").IsFalse());
        m.Dead = !live; m.View = view; m.Why = why;
        m.WhatText = m.What(d.At("detail") is not null ? d : null);
        // Claude Code and Codex modules are titled with the model that is running, coloured by its tier.
        if (m.Family is not null)
        {
            // The GPU and CPU modules are titled with the hardware's own name (no tier colour).
            var ai = m.Model is null; var x = d.At("detail");
            var model = ai ? x.At("model").Str() : x is null ? null : m.Model!(d!);
            var has = !string.IsNullOrEmpty(model);
            m.TitleText = has ? model! : m.Family;
            m.Tier = ai && has ? (int)(x.At("model_tier").Num() ?? 0) : 0;
            var effort = ai && has ? x.At("effort").Str() : null;
            m.EffortText = string.IsNullOrEmpty(effort) ? "" : effort.ToUpperInvariant() + " EFFORT";
            m.EffortTier = (int)(x.At("effort_tier").Num() ?? 0);
            if (has) m.WhatText = m.Family + ", " + m.WhatText;
        }

        // The burn flash: first time the state leaves IDLE after at least 60 s of rest.
        if (live)
        {
            if (view == "IDLE") { if (m.Prev != "IDLE") m.IdleSince = now; }
            else if (m.Prev == "IDLE" && now - m.IdleSince >= RestMs) m.FlashAt = now;
            m.Prev = view;
        }

        var gone = dl is null || view is "OFFLINE" or "CONNECTING";
        m.NeedleAlpha = gone ? 0 : m.Dead ? .35 : 1;   // a dead source has no needle position at all
        if (gone) { m.NumText = "\u2014"; m.RingTarget = 0; }
        if (dl is null || d is null)
        {
            m.SparkData.Clear(); m.RowData.Clear(); m.CapText = m.UnitText = m.HiText = ""; m.BandFrom = null;
            return;
        }
        if (dl.Value is double v && view != "OFFLINE")
        {
            m.NeedleTarget = Catalog.Frac(v, dl.ScaleMax, dl.Log);
            m.NumText = Catalog.F(v, m.Digits);
        }
        else if (dl.Value is null) { m.NeedleAlpha = 0; m.NumText = "\u2014"; }
        m.UnitText = m.Unit is not null ? m.Unit(d) : dl.Label;
        m.HiText = dl.ScaleMax is null ? "" : Catalog.F(dl.ScaleMax, 0);
        m.BandFrom = dl.Redline is not null && dl.ScaleMax is > 0 ? Catalog.Frac(dl.Redline, dl.ScaleMax, dl.Log) : null;
        // Inner ring: a second reading where the module has one (prompt tokens, live output, load).
        if (view != "OFFLINE") m.RingTarget = m.Ring?.Invoke(d) ?? 0;

        m.SparkData = m.Spark is not null ? m.Spark(d) : d.At("detail", "spark").Nums();
        m.SparkTop = m.SparkData.Count > 0 ? Math.Max(m.SparkFloor, m.SparkData.Max()) : m.SparkFloor;
        m.CapText = m.SparkData.Count > 0
            ? $"last {m.SparkData.Count} {m.SparkWord}, peak {Catalog.F(m.SparkData.Max(), m.Digits)} {m.SparkUnit}" : "";
        m.RowData = m.Rows(d);
    }

    // ---- motion --------------------------------------------------------------------------------------------

    // The needle is a damped spring toward the newest reading, stepped every frame: it glides between the
    // one-per-second samples and settles with a small overshoot, like a real gauge.
    void Animate()
    {
        var now = _clock.Elapsed.TotalMilliseconds;
        var dt = Math.Min((now - _lastTick) / 1000, 0.05);
        _lastTick = now;
        var hidden = WindowState == FormWindowState.Minimized;
        foreach (var m in _modules)
        {
            var moving = false;
            for (var left = dt; left > 0; left -= 0.004)   // small steps keep the spring stable
            {
                var h = Math.Min(left, 0.004);
                m.NeedleVel += (150 * (m.NeedleTarget - m.NeedlePos) - 17 * m.NeedleVel) * h;
                m.NeedlePos += m.NeedleVel * h;
            }
            if (Math.Abs(m.NeedleTarget - m.NeedlePos) > 0.0015 || Math.Abs(m.NeedleVel) > 0.01) moving = true;   // under a pixel: settled
            else { m.NeedlePos = m.NeedleTarget; m.NeedleVel = 0; }
            var ringStep = (m.RingTarget - m.RingPos) * (1 - Math.Exp(-9 * dt));
            if (Math.Abs(m.RingTarget - m.RingPos) > 0.002) { m.RingPos += ringStep; moving = true; } else m.RingPos = m.RingTarget;
            if (now - m.FlashAt < 350) moving = true;
            foreach (var b in m.Buttons)
                if (b.Armed && now > b.ArmUntil) { b.Armed = false; Invalidate(Rectangle.Ceiling(b.Rect)); }
            if (moving && !hidden) { Update(); Invalidate(Rectangle.Ceiling(m.Face)); Update(); }
        }
    }

    // ---- buttons -------------------------------------------------------------------------------------------

    Btn? Hit(Point p, out Module? owner)
    {
        foreach (var m in _modules) foreach (var b in m.Buttons) if (b.Rect.Contains(p)) { owner = m; return b; }
        owner = null; return null;
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        var hit = Hit(e.Location, out _);
        Cursor = hit is { Busy: false } ? Cursors.Hand : Cursors.Default;
        foreach (var m in _modules) foreach (var b in m.Buttons)
            if (b.Hover != (b == hit)) { b.Hover = b == hit; Invalidate(Rectangle.Ceiling(b.Rect)); }
    }

    protected override void OnMouseLeave(EventArgs e) => OnMouseMove(new MouseEventArgs(MouseButtons.None, 0, -1, -1, 0));

    // Kill switch: the first click arms the button for three seconds, the second one fires.
    protected override async void OnMouseClick(MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left || Hit(e.Location, out var m) is not { Busy: false } b || m is null) return;
        if (!b.Armed)
        {
            b.Armed = true; b.ArmUntil = _clock.Elapsed.TotalMilliseconds + 3000;
            Invalidate(Rectangle.Ceiling(b.Rect));
            return;
        }
        b.Armed = false; b.Busy = true; Invalidate(Rectangle.Ceiling(m.Card));
        string msg;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, _base + "kill?target=" + Uri.EscapeDataString(b.Target));
            req.Headers.Add("X-Gauge-Kill", "1");
            using var cts = new CancellationTokenSource(30000);
            using var r = await _http.SendAsync(req, cts.Token);
            var j = JsonNode.Parse(await r.Content.ReadAsStringAsync(cts.Token));
            msg = j.At("message").Str() ?? j.At("error").Str() ?? "HTTP " + (int)r.StatusCode;
        }
        catch (Exception ex) { msg = "request failed: " + (ex.InnerException?.Message ?? ex.Message); }
        m.KillMsg = DateTime.Now.ToString("HH:mm:ss") + "  " + msg;
        b.Busy = false; Invalidate(Rectangle.Ceiling(m.Card));
    }

    // ---- drawing -------------------------------------------------------------------------------------------

    void Fonts(float em)
    {
        if (Math.Abs(em - _em) < 0.05f && _fBody is not null) return;
        _em = em;
        foreach (var f in new[] { _fBody, _fSmall, _fRows, _fRowsBold, _fH1, _fH2, _fBtn }) f?.Dispose();
        _fBody = new Font(_mono, em, FontStyle.Regular, GraphicsUnit.Pixel);
        _fSmall = new Font(_mono, em * .9f, FontStyle.Regular, GraphicsUnit.Pixel);
        _fRows = new Font(_mono, em * .93f, FontStyle.Regular, GraphicsUnit.Pixel);
        _fRowsBold = new Font(_mono, em * .93f, FontStyle.Bold, GraphicsUnit.Pixel);
        _fBtn = new Font(_mono, em * .9f, FontStyle.Bold, GraphicsUnit.Pixel);
        _fH1 = new Font(_serif, em * 2f, FontStyle.Regular, GraphicsUnit.Pixel);
        _fH2 = new Font(_serif, em * 1.5f, FontStyle.Regular, GraphicsUnit.Pixel);
    }

    static readonly StringFormat LeftF = new(StringFormat.GenericTypographic) { FormatFlags = 0 },
        RightF = new(StringFormat.GenericTypographic) { FormatFlags = 0, Alignment = StringAlignment.Far },
        Center = new(StringFormat.GenericTypographic) { FormatFlags = 0, Alignment = StringAlignment.Center },
        Mid = new(StringFormat.GenericTypographic) { FormatFlags = StringFormatFlags.NoWrap, Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center },
        Base = new(StringFormat.GenericTypographic) { FormatFlags = StringFormatFlags.NoWrap | StringFormatFlags.NoClip, Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Far };

    // Draws wrapped text in a column and returns the height it took.
    static float Draw(Graphics g, string s, Font f, Color c, float x, float y, float w, StringFormat fmt, float minLines = 0)
    {
        var min = minLines * f.Height;
        if (string.IsNullOrEmpty(s)) return min;
        var h = g.MeasureString(s, f, new SizeF(w, 4000), fmt).Height;
        using var b = new SolidBrush(c);
        g.DrawString(s, f, b, new RectangleF(x, y, w, h + 1), fmt);
        return Math.Max(h, min);
    }

    static GraphicsPath Round(RectangleF r, float rad)
    {
        var p = new GraphicsPath(); var d = rad * 2;
        p.AddArc(r.X, r.Y, d, d, 180, 90); p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90); p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure(); return p;
    }

    void Arrange(Graphics g)
    {
        float k = DeviceDpi / 96f, w = ClientSize.Width, h = ClientSize.Height;
        Fonts(Math.Clamp(1.16f * w / k / 100f, 9.5f, 13f) * k);   // the page's clamp(9.5px, 1.16vw, 13px)
        float gap = 6 * k, side = 6 * k;
        var subH = g.MeasureString(Sub, _fBody, new SizeF(w - 20 * k, 4000), Center).Height;
        _header = new RectangleF(0, 0, w, 6 * k + _fH1.Height + subH + 5 * k);
        var footH = g.MeasureString(Foot, _fSmall, new SizeF(w - 20 * k, 4000), Center).Height;
        _footer = new RectangleF(0, h - footH - 8 * k, w, footH + 8 * k);
        int cols = w < 620 * k ? 2 : 4, rows = _modules.Count / cols;
        float cw = (w - 2 * side - (cols - 1) * gap) / cols, ch = (_footer.Y - _header.Bottom - (rows - 1) * gap) / rows;
        for (var i = 0; i < _modules.Count; i++)
            _modules[i].Card = new RectangleF(side + (i % cols) * (cw + gap), _header.Bottom + (i / cols) * (ch + gap), cw, ch);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
        // A moving needle invalidates only its dial face: redraw that and nothing else.
        foreach (var m in _modules)
            if (m.FaceScale > 0 && Rectangle.Ceiling(m.Face).Contains(e.ClipRectangle))
            { PaintCard(g, m, DeviceDpi / 96f, true); return; }
        g.Clear(Bg);
        Arrange(g);
        float k = DeviceDpi / 96f, w = ClientSize.Width;
        if (e.ClipRectangle.IntersectsWith(Rectangle.Ceiling(_header)))
        {
            using var t = new SolidBrush(Txt);
            g.DrawString("Token-burn gauge", _fH1, t, new RectangleF(0, 6 * k, w, _fH1.Height + 2), Center);
            Draw(g, Sub, _fBody, Dim, 10 * k, 6 * k + _fH1.Height, w - 20 * k, Center);
        }
        if (e.ClipRectangle.IntersectsWith(Rectangle.Ceiling(_footer)))
            Draw(g, Foot, _fSmall, Dim, 10 * k, _footer.Y + 4 * k, w - 20 * k, Center);
        foreach (var m in _modules)
            if (e.ClipRectangle.IntersectsWith(Rectangle.Ceiling(m.Card))) PaintCard(g, m, k, false);
    }

    void PaintCard(Graphics g, Module m, float k, bool faceOnly)
    {
        var r = m.Card; var c = m.Dead ? DeadC : StateColor(m.View);
        if (faceOnly && m.FaceScale > 0)
        {
            PaintFace(g, m, c, m.FaceX, m.FaceY, m.FaceScale);
            return;
        }
        using (var path = Round(new RectangleF(r.X + .5f, r.Y + .5f, r.Width - 1, r.Height - 1), 9 * k))
        using (var fill = new SolidBrush(Panel)) using (var edge = new Pen(Line, 1))
        { g.FillPath(fill, path); g.DrawPath(edge, path); }
        var saved = g.Save();
        g.SetClip(RectangleF.Inflate(r, -2, -2), CombineMode.Intersect);

        float pad = 8 * k, x = r.X + pad, w = r.Width - 2 * pad, y = r.Y + 6 * k, bottom = r.Bottom - 7 * k;

        // Buttons sit at the bottom of the card, whatever the rows above them need.
        if (m.Buttons.Count > 0)
        {
            if (m.KillMsg.Length > 0)
            {
                var mh = g.MeasureString(m.KillMsg, _fSmall, new SizeF(w, 4000), LeftF).Height;
                Draw(g, m.KillMsg, _fSmall, Dim, x, bottom - mh, w, LeftF); bottom -= mh + 2 * k;
            }
            float bh = _fBtn.Height + 7 * k;
            for (var i = m.Buttons.Count - 1; i >= 0; i--)
            {
                var b = m.Buttons[i];
                b.Rect = new RectangleF(x, bottom - bh, w, bh); bottom -= bh + 3 * k;
                var edge = b.Resume ? Slate : Redline; var on = (b.Hover || b.Armed) && !b.Busy;
                var alpha = b.Busy ? .5 : 1;
                using var bp = Round(b.Rect, 6 * k);
                if (on) { using var fb = new SolidBrush(edge); g.FillPath(fb, bp); }
                using var pen = new Pen(A(edge, alpha), 1); g.DrawPath(pen, bp);
                using var tb = new SolidBrush(A(on ? Color.White : b.Resume ? Txt : Redline, alpha));
                g.DrawString(b.Busy ? "WORKING" : b.Armed ? "CLICK AGAIN TO CONFIRM" : b.Label, _fBtn, tb, b.Rect, Mid);
            }
            bottom -= k;
        }

        y += Draw(g, m.TitleText.Length > 0 ? m.TitleText : m.Title, _fH2, TierColor(m.Tier), x, y, w, LeftF, 1) * 1.02f;
        if (m.EffortText.Length > 0)
            y += Draw(g, m.EffortText, _fBtn, m.EffortTier > 0 ? TierColor(m.EffortTier) : Dim, x, y, w, LeftF);
        y += Draw(g, m.WhatText, _fBody, Dim, x, y, w, LeftF, 1);

        // The dial face keeps the page's 264 by 226 proportions.
        float fw = Math.Min(w, r.Height * .38f * 264 / 226), fh = fw * 226 / 264;
        m.Face = new RectangleF((float)Math.Floor(x + (w - fw) / 2), (float)Math.Floor(y), (float)Math.Ceiling(fw), (float)Math.Ceiling(fh));
        m.FaceX = (float)Math.Floor(x + (w - fw) / 2); m.FaceY = (float)Math.Floor(y); m.FaceScale = fw / 264;
        PaintFace(g, m, c, m.FaceX, m.FaceY, m.FaceScale);
        y += fh;
        y += Draw(g, m.UnitText, _fBody, Dim, x, y, w, Center, 1);

        // Sparkline: the last minute of samples.
        float sh = 1.7f * _em; y += 2 * k;
        if (m.SparkData.Count > 1)
        {
            var pts = new PointF[m.SparkData.Count];
            for (var i = 0; i < pts.Length; i++)
                pts[i] = new PointF(x + w * i / 59f, y + sh * (36 - 34 * (float)(m.SparkData[i] / m.SparkTop)) / 38);
            using var sp = new Pen(m.Dead ? A(Dim, .4) : c, 1.5f * k) { LineJoin = LineJoin.Round };
            g.DrawLines(sp, pts);
        }
        y += sh;
        y += Draw(g, m.CapText, _fSmall, Dim, x, y, w, LeftF);
        y += Draw(g, m.Why, _fBody, Dim, x, y, w, LeftF);

        if (m.RowData.Count > 0)
        {
            y += 3 * k;
            using (var lp = new Pen(Line, 1)) g.DrawLine(lp, x, y, x + w, y);
            y += 3 * k;
            g.SetClip(new RectangleF(r.X, y, r.Width, Math.Max(0, bottom - y)), CombineMode.Intersect);
            var fade = m.Dead ? .45 : 1;
            foreach (var row in m.RowData)
            {
                if (row.Bar is double pct)
                {
                    float bh = 4 * k; var track = new RectangleF(x, y + 2 * k, w, bh);
                    using var tb = new SolidBrush(Hex(0x121925)); g.FillRectangle(tb, track);
                    using var fb = new SolidBrush(m.Dead ? A(Dim, .4) : c);
                    g.FillRectangle(fb, x, track.Y, w * (float)Math.Clamp(pct / 100, 0, 1), bh);
                    y += bh + 3 * k; continue;
                }
                var vf = row.Low ? _fRowsBold : _fRows;
                var vw = Math.Min(g.MeasureString(row.V, vf, new SizeF(4000, 4000), LeftF).Width + 2, w * .62f);
                var kw = Math.Max(w - vw - 6 * k, w * .3f);
                var kh = Draw(g, row.K, _fRows, A(Dim, fade), x, y, kw, LeftF);
                var vh = Draw(g, row.V, vf, A(row.Low ? Redline : Txt, fade), x + w - vw, y, vw, RightF);
                y += Math.Max(Math.Max(kh, vh), _fRows.Height) + k;
            }
        }
        g.Restore(saved);
    }

    static PointF Polar(float r, double deg)
    {
        var a = (deg - 90) * Math.PI / 180;
        return new PointF(CX + r * (float)Math.Cos(a), CY + r * (float)Math.Sin(a));
    }

    static void Arc(Graphics g, Pen p, float r, double fromDeg, double sweepDeg)
    {
        if (sweepDeg > 0.05) g.DrawArc(p, CX - r, CY - r, 2 * r, 2 * r, (float)(fromDeg - 90), (float)sweepDeg);
    }

    void PaintFace(Graphics g, Module m, Color c, float ox, float oy, float s)
    {
        // The parts that never move (panel, ticks, ring track) are drawn once into a bitmap and copied.
        g.DrawImageUnscaled(StaticFace(s), (int)ox, (int)oy);
        var saved = g.Save();
        g.TranslateTransform(ox - 28 * s, oy - 32 * s);
        g.ScaleTransform(s, s);
        if (m.BandFrom is double bf)
        {
            using var band = new Pen(A(Redline, .75), 5);
            Arc(g, band, R + 5, Start + Sweep * bf, Sweep * (1 - bf));
        }

        // Inner ring, with the burn flash: wide and white, easing back to the state colour.
        var flash = Math.Clamp((_clock.Elapsed.TotalMilliseconds - m.FlashAt) / 300, 0, 1);
        var ringColor = flash < 1 ? Color.FromArgb(255, Mix(255, c.R, flash), Mix(255, c.G, flash), Mix(255, c.B, flash)) : c;
        using (var ring = new Pen(ringColor, (float)(20 - 13 * flash)) { StartCap = LineCap.Round, EndCap = LineCap.Round })
            Arc(g, ring, 92, Start, Sweep * m.RingPos);

        if (m.NeedleAlpha > 0)
        {
            using var needle = new Pen(A(c, m.NeedleAlpha), 3) { StartCap = LineCap.Round, EndCap = LineCap.Round };
            g.DrawLine(needle, new PointF(CX, CY), Polar(104, Start + Sweep * Math.Clamp(m.NeedlePos, -0.02, 1.02)));
        }
        using (var hub = new SolidBrush(c)) g.FillEllipse(hub, CX - 6, CY - 6, 12, 12);

        using (var num = new SolidBrush(A(Txt, m.Dead ? .45 : 1))) g.DrawString(m.NumText, _fNum, num, CX, 229, Base);
        using (var st = new SolidBrush(c)) g.DrawString(string.Join("\u200A", m.View.ToCharArray()), _fState, st, CX, 257, Base);
        using (var dim = new SolidBrush(Dim))
        {
            g.DrawString("0", _fScale, dim, 54, 243, Base);
            g.DrawString(m.HiText, _fScale, dim, 266, 243, Base);
        }
        g.Restore(saved);
    }

    Bitmap? _static; float _staticScale;

    Bitmap StaticFace(float s)
    {
        if (_static is not null && _staticScale == s) return _static;
        _static?.Dispose();
        _static = new Bitmap((int)Math.Ceiling(264 * s), (int)Math.Ceiling(226 * s), System.Drawing.Imaging.PixelFormat.Format32bppPArgb);
        _staticScale = s;
        using var g = Graphics.FromImage(_static);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Panel);
        g.TranslateTransform(-28 * s, -32 * s);
        g.ScaleTransform(s, s);
        using var minor = new Pen(Hex(0x2b3546), 1.5f); using var major = new Pen(Slate, 2);
        for (var i = 0; i <= 40; i++)
        {
            var d = Start + Sweep * i / 40f; var big = i % 5 == 0;
            g.DrawLine(big ? major : minor, Polar(R, d), Polar(R - (big ? 13 : 7), d));
        }
        using var ringBg = new Pen(Hex(0x121925), 7); Arc(g, ringBg, 92, Start, Sweep);
        return _static;
    }

    static int Mix(int from, int to, double t) => (int)Math.Round(from + (to - from) * t);
}
