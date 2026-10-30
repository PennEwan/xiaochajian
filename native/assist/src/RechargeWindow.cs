using System;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace CodexNativeAssist
{
    // The native CDK window, drawn as the Codex 小助手 card: borderless rounded card, aurora background,
    // draggable, Esc closes (see Card.cs). Plain WinForms from .NET Framework, real Win32 controls.
    // Prints one CDK-free JSON line when it closes; that is all the MCP tool (and Codex) ever sees.
    sealed class RechargeWindow : Form
    {
        const string RedeemText = "确认提交", ReloadText = "重新读取";
        const string SessionUrl = "https://chatgpt.com/api/auth/session";

        readonly RechargeFlow _flow = new RechargeFlow();
        readonly TextBox _account = new TextBox();
        readonly Label _status = new Label(), _footer = new Label();
        readonly LinkLabel _link = new LinkLabel();
        readonly TextBox _cdk = new TextBox();
        readonly PillButton _action = new PillButton(), _close = new PillButton();
        readonly PillButton _clip = new PillButton();
        readonly BrowserSessionBridge _browserBridge;
        readonly float _s;
        readonly Bitmap _aurora;
        readonly Font _small, _smallBold, _tiny;
        volatile bool _busy, _redeeming, _readingBrowser;
        string _state;
        bool _live;
        Outcome _last;

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, string l);
        [DllImport("user32.dll")]
        static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
        [DllImport("user32.dll")]
        static extern bool ReleaseCapture();
        [DllImport("gdi32.dll")]
        static extern IntPtr CreateRoundRectRgn(int x1, int y1, int x2, int y2, int w, int h);
        [DllImport("gdi32.dll")]
        static extern bool DeleteObject(IntPtr h);

        public static int Run()
        {
            bool noWait = Environment.GetCommandLineArgs().Length > 2 && Environment.GetCommandLineArgs()[2] == "--no-wait";
            using (var single = new SingleInstance("recharge"))
            {
                if (!single.First)
                {
                    TryWrite("{\"status\":\"already_open\"}");
                    return 0;
                }
                var w = new RechargeWindow();
                single.OnActivate(w);
                w.Shown += delegate { TryWrite("{\"status\":\"opened\"}"); };
                Application.Run(w);
                if (!noWait) TryWrite(w.ResultLine());
                return 0;
            }
        }

        static void TryWrite(string line)
        {
            try { Cli.WriteLine(line); } catch (Exception) { } // nobody listening (wait=false): fine
        }

        string ResultLine()
        {
            if (_last == null) return Json.Stringify(Json.D("status", "cancelled"));
            string status = _last.Ok ? _last.Kind : _last.Kind == "processing" ? "processing" : "failed";
            // Status words and product only — no CDK, no masked CDK, no account data.
            return Json.Stringify(Json.D("status", status, "kind", _last.Kind, "product", _last.Product));
        }

        int P(double v) { return (int)Math.Round(v * _s); }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ClassStyle |= 0x00020000; // CS_DROPSHADOW: the shadow Windows draws for borderless popups
                return cp;
            }
        }

        RechargeWindow()
        {
            _browserBridge = new BrowserSessionBridge();
            Text = "Codex 充值";
            using (var g = CreateGraphics()) _s = g.DpiX / 96f;
            Font = new Font(Card.Family, 9f);
            _small = new Font(Card.Family, 9f);
            _smallBold = new Font(Card.Family, 9f, FontStyle.Bold);
            _tiny = new Font(Card.Family, 8f);
            AutoScaleMode = AutoScaleMode.None;
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = true;
            DoubleBuffered = true;
            BackColor = Card.Bg;
            KeyPreview = true;
            TopMost = true;
            _aurora = Card.Aurora(Card.Width, Card.MaxHeight, _s);
            ClientSize = new Size(P(Card.Width), P(330));

            int right = P(Card.Width - Card.PadX);
            _close.Backdrop = _action.Backdrop = _aurora;
            _close.Text = "关闭";
            _close.Glyph = "✕";
            _close.Fill = Color.White;
            _close.Hot = Color.FromArgb(236, 242, 248);
            _close.InkColor = Card.Ink;
            _close.Font = _small;
            _close.TabStop = false;
            _close.Bounds = new Rectangle(right - P(24), P(Card.PadTop), P(24), P(24));

            _account.BorderStyle = BorderStyle.None;
            _account.BackColor = Card.WellBg;
            _account.ForeColor = Card.Ink;
            _account.Font = new Font(Card.Family, 11f);
            _account.MaxLength = 256;
            _account.ShortcutsEnabled = true;
            _account.Location = new Point(P(Card.PadX + 13), P(Card.PadTop + 92) - _account.PreferredHeight / 2);
            _account.Width = P(Card.Width - 2 * Card.PadX - 26);

            _cdk.BorderStyle = BorderStyle.None;
            _cdk.BackColor = Card.WellBg;
            _cdk.ForeColor = Card.Ink;
            _cdk.Font = new Font(Card.Family, 11f);
            _cdk.MaxLength = 200;
            _cdk.ShortcutsEnabled = true;
            _cdk.Location = new Point(P(Card.PadX + 13), P(Card.PadTop + 149) - _cdk.PreferredHeight / 2);
            _cdk.Width = P(Card.Width - 2 * Card.PadX - 26);

            _clip.Text = "从剪贴板读取";
            _clip.Fill = Color.White;
            _clip.Hot = Color.FromArgb(236, 242, 248);
            _clip.InkColor = Card.Ink;
            _clip.Font = _small;
            _clip.TabStop = false;
            _clip.Visible = false;
            _clip.Click += delegate { ReadClipboardSession(); };
            _action.Font = new Font(Card.Family, 10f, FontStyle.Bold);
            _action.Text = ReloadText;

            Plain(_status, _small, Card.Muted);
            Plain(_footer, _tiny, Card.Faint);
            _link.AutoSize = false;
            _link.Text = SessionUrl;
            _link.Size = new Size(P(Card.Width - 2 * Card.PadX), P(30));
            _link.Font = _tiny;
            _link.BackColor = Color.Transparent;
            _link.LinkColor = Card.Blue;
            _link.ActiveLinkColor = Card.BlueHot;
            _link.VisitedLinkColor = Card.Blue;
            _link.LinkArea = new LinkArea(0, _link.Text.Length);
            _link.LinkClicked += delegate { ReadSessionFromBrowser(); };
            foreach (var l in new[] { _status, _footer })
            {
                l.AutoSize = true;
                l.MaximumSize = new Size(P(Card.Width - 2 * Card.PadX), 0);
            }
            _status.Text = "请输入充值账号和 CDK。";
            _footer.Text = "账号会记住，下次只需粘贴 CDK；下方链接与浏览器扩展是可选的。";

            Controls.AddRange(new Control[] { _close, _account, _cdk, _action, _clip, _status, _footer, _link });
            CancelButton = _close;
            _close.Click += delegate { Close(); };
            _action.Click += delegate { OnAction(); };
            _account.KeyDown += (s, e) => { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; _cdk.Focus(); } };
            _cdk.KeyDown += (s, e) => { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; DoRedeem(); } };
            _account.TextChanged += delegate { SetInputs(); };
            _cdk.TextChanged += delegate { SetInputs(); };
            foreach (Control c in new Control[] { this, _status, _footer }) c.MouseDown += DragCard;
            FormClosing += OnClosingConfirm;
            FormClosed += delegate { _browserBridge.Dispose(); _aurora.Dispose(); };
            Shown += delegate
            {
                Activate();
                TopMost = false;
                SendMessage(_account.Handle, 0x1501 /* EM_SETCUEBANNER */, (IntPtr)1, "输入充值账号");
                SendMessage(_cdk.Handle, 0x1501 /* EM_SETCUEBANNER */, (IntPtr)1, "粘贴或输入 CDK");
            };
            Load += delegate
            {
                string last = Settings.LastAccount();
                if (!string.IsNullOrEmpty(last) && _account.Text.Trim().Length == 0) _account.Text = last;
                var area = Screen.FromPoint(Cursor.Position).WorkingArea;
                Location = new Point(area.X + (area.Width - Width) / 2, area.Y + (area.Height - Height) / 3);
                Work(() => _flow.Load(), ShowView);
            };
            _busy = true;
            SetInputs();
        }

        static void Plain(Label l, Font f, Color c)
        {
            l.Font = f;
            l.ForeColor = c;
            l.BackColor = Color.Transparent;
            l.UseMnemonic = false;
        }

        // ---- the card ------------------------------------------------------------------------

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.DrawImageUnscaled(_aurora, 0, 0);
            const TextFormatFlags left = TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding | TextFormatFlags.SingleLine;
            int x = P(Card.PadX), right = P(Card.Width - Card.PadX);
            TextRenderer.DrawText(g, "C O D E X", _smallBold, new Rectangle(x, P(Card.PadTop), P(90), P(24)), Card.Ink, left);
            TextRenderer.DrawText(g, "充值", _small, new Rectangle(x + P(74), P(Card.PadTop), P(60), P(24)), Card.Muted, left);
            float dot = _close.Left + _close.Width / 2f - P(24);
            using (var b = new SolidBrush(_live ? Card.StatusOk : Card.StatusStale))
            {
                g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                g.FillEllipse(b, dot - P(4), P(Card.PadTop + 8), P(8), P(8));
            }
            TextRenderer.DrawText(g, "充值账号", _small, new Rectangle(x, P(Card.PadTop + 38), P(120), P(24)), Card.Faint, left);
            Card.FillRound(g, Card.WellBg, new RectangleF(x, P(Card.PadTop + 66), right - x, P(38)), P(11), Card.WellEdge);
            TextRenderer.DrawText(g, "CDK", _small, new Rectangle(x, P(Card.PadTop + 116), P(120), P(24)), Card.Faint, left);
            Card.FillRound(g, Card.WellBg, new RectangleF(x, P(Card.PadTop + 130), right - x, P(38)), P(11), Card.WellEdge);
        }

        // Everything below the CDK box follows the status text: the card grows with it.
        void Relayout()
        {
            int right = P(Card.Width - Card.PadX);
            using (var g = CreateGraphics())
            {
                int w = Math.Max(P(96), TextRenderer.MeasureText(g, _action.Text, _action.Font).Width + P(36));
                _action.Bounds = new Rectangle(right - w, P(Card.PadTop + 182), w, P(36));
            }
            int y = _action.Bottom + P(18);
            _status.Location = new Point(P(Card.PadX), y);
            if (_status.Text.Length > 0) y = _status.Bottom + P(14);
            _footer.Location = new Point(P(Card.PadX), y);
            _link.Location = new Point(P(Card.PadX), _footer.Bottom + P(4));
            int height = Math.Min(_link.Bottom + P(Card.PadBottom), _aurora.Height);
            if (ClientSize.Height != height || Region == null)
            {
                ClientSize = new Size(P(Card.Width), height);
                IntPtr rgn = CreateRoundRectRgn(0, 0, Width + 1, height + 1, P(Card.Radius * 2), P(Card.Radius * 2));
                Region = Region.FromHrgn(rgn);
                DeleteObject(rgn);
            }
            Invalidate(true);
        }

        void DragCard(object sender, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left) return;
            ReleaseCapture();
            SendMessage(Handle, 0xA1 /* WM_NCLBUTTONDOWN */, (IntPtr)2 /* HTCAPTION */, IntPtr.Zero);
        }

        // ---- states --------------------------------------------------------------------------

        string Mode
        {
            get
            {
                if (_state == RechargeFlow.Ready) return RedeemText;
                return ReloadText;
            }
        }

        void Say(string text, Color color)
        {
            _status.ForeColor = color;
            _status.Text = text;
            Relayout();
        }

        void ShowView(View v)
        {
            _state = v.State;
            _live = v.State == RechargeFlow.Ready;
            bool problem = v.State == RechargeFlow.NotConfigured || v.State == RechargeFlow.NoAccount;
            Say(v.Message, problem ? Card.BadRed : Card.Muted);
            SetInputs();
        }

        void SetInputs()
        {
            string mode = Mode;
            bool redeem = mode == RedeemText;
            _action.Text = mode;
            _action.Enabled = !_busy && (!redeem || (_account.Text.Trim().Length > 0 && _cdk.Text.Trim().Length > 0));
            _account.ReadOnly = !redeem || _busy;
            _account.TabStop = redeem && !_busy;
            _account.ForeColor = redeem && !_busy ? Card.Ink : Card.Faint;
            bool editable = redeem && !_busy;
            _cdk.ReadOnly = !editable;
            _cdk.TabStop = editable;
            _cdk.ForeColor = editable ? Card.Ink : Card.Faint;
            if (editable && !_account.Focused && !_cdk.Focused) _account.Focus();
            Relayout();
        }

        void OnAction()
        {
            if (_busy) return;
            string mode = Mode;
            if (mode == RedeemText) DoRedeem();
            else { Say("正在检查充值服务…", Card.Muted); Work(() => _flow.Load(), ShowView); }
        }

        void DoRedeem()
        {
            if (_busy || Mode != RedeemText || _account.Text.Trim().Length == 0 || _cdk.Text.Trim().Length == 0) return;
            string account = _account.Text, cdk = _cdk.Text;
            Say("正在提交…", Card.Muted);
            _redeeming = true;
            Work(() => _flow.Redeem(cdk, account, msg => BeginInvoke((Action)(() => Say(msg, Card.Muted)))),
                 o => { _redeeming = false; ShowOutcome(o); });
        }

        void ReadBrowserSession()
        {
            if (_busy) return;
            _readingBrowser = true;
            Say("正在读取浏览器登录内容…", Card.Muted);
            Work(() => _browserBridge.Request(), ShowBrowserSession);
        }

        static bool CopyText(string text)
        {
            for (int i = 0; i < 4; i++)
            {
                try
                {
                    Clipboard.SetText(text, TextDataFormat.UnicodeText);
                    return true;
                }
                catch (ExternalException)
                {
                    if (i < 3) Thread.Sleep(25);
                }
                catch (ArgumentException) { return false; }
            }
            return false;
        }

        // One click, no extension: try the bridge (if the extension is installed it answers in ms); when
        // nothing is polling, open the session URL in the default browser and let the user copy the page.
        void ReadSessionFromBrowser()
        {
            if (_busy) return;
            _readingBrowser = true;
            Say("正在读取登录账号…", Card.Muted);
            Work(() =>
            {
                // 1) Codex's own login file already carries the account: instant, local, no browser.
                string error;
                var account = LocalAccount.Read(out error);
                if (account == null) account = CodexAccount.Read(out error);
                if (account != null)
                    return new BrowserSessionResult { Ok = true, Content = AccountJson(account), Error = null };
                // 2) the browser extension, when it is installed
                var bridged = _browserBridge.Request(1500);
                if (bridged.Ok || bridged.Error == "busy") return bridged;
                // 3) last resort: open the page and let the user copy it
                try { Process.Start(new ProcessStartInfo(SessionUrl) { UseShellExecute = true }); }
                catch (Exception) { }
                return new BrowserSessionResult { Error = "opened_browser" };
            }, ShowBrowserSession);
        }

        // Only email / account id / plan ever leave CodexAccount; the token is never touched.
        static string AccountJson(Account account)
        {
            return Json.Stringify(Json.D("email", account.Email, "id", account.Id, "plan", account.Plan));
        }

        // Fallback: the page shows the session JSON; Ctrl+A / Ctrl+C, then this reads it.
        void ReadClipboardSession()
        {
            string text = null;
            try { text = Clipboard.ContainsText() ? Clipboard.GetText(TextDataFormat.UnicodeText) : null; }
            catch (Exception) { }
            string account, error;
            if (TryParseSession(text, out account, out error))
            {
                _account.Text = account;               // only the account survives; tokens are dropped
                Say("已从剪贴板读取登录账号。", Card.OkGreen);
            }
            else
            {
                Say(error, Card.BadRed);
            }
            SetInputs();
        }

        // The session JSON carries an access token: take the account fields and keep nothing else.
        static bool TryParseSession(string text, out string account, out string error)
        {
            account = null;
            error = null;
            if (string.IsNullOrWhiteSpace(text) || text.IndexOf('{') < 0)
            {
                error = "剪贴板里没有登录内容，请先在浏览器打开 session 链接并复制整页。";
                return false;
            }
            var doc = Json.ParseObject(text);
            if (doc == null) { error = "剪贴板内容不是有效的 JSON。"; return false; }
            // Two shapes: the browser session ({user:{email,id}}) and our own local account JSON
            // ({email,id,plan}).
            var user = Json.Obj(doc, "user");
            string email = Json.Str(user, "email");
            string id = Json.Str(user, "id");
            if (string.IsNullOrEmpty(email)) email = Json.Str(doc, "email");
            if (string.IsNullOrEmpty(id)) id = Json.Str(doc, "id");
            if (string.IsNullOrEmpty(email) && string.IsNullOrEmpty(id))
            {
                error = "这段内容里没有账号信息，可能没有登录 ChatGPT。";
                return false;
            }
            account = string.IsNullOrEmpty(email) ? id : email;
            return true;
        }

        void ShowBrowserSession(BrowserSessionResult result)
        {
            _readingBrowser = false;
            if (result.Ok)
            {
                string account, parseError;
                if (TryParseSession(result.Content, out account, out parseError) && _account.Text.Trim().Length == 0)
                    _account.Text = account;
            }
            bool copied = result.Ok && CopyText(result.Content);
            if (copied)
                Say("登录信息已复制到剪贴板，账号已填好。", Card.OkGreen);
            else if (result.Ok)
                Say("无法写入剪贴板，请稍后重试。", Card.BadRed);
            else if (result.Error == "not_logged_in" || result.Error == "empty")
                Say("未检测到登录内容，请先登录 ChatGPT 后重试。", Card.BadRed);
            else if (result.Error == "opened_browser")
            {
                _clip.Visible = true;
                Say("已在浏览器打开 session 页面：Ctrl+A 复制整页，剪贴板会留在本机。", Card.Muted);
                SetInputs();
                return;
            }
            else if (result.Error == "bridge_unavailable")
                Say("自动读取服务未启动，请重新打开充值窗口。", Card.BadRed);
            else if (result.Error == "busy")
                Say("正在读取浏览器登录内容，请稍候。", Card.Muted);
            else
                Say("读取超时，请确认浏览器扩展已安装并已登录 ChatGPT。", Card.BadRed);
            SetInputs();
        }

        void ShowOutcome(Outcome o)
        {
            _last = o;
            Say(o.Message, o.Ok ? Card.OkGreen : o.Kind == "processing" ? Card.Muted : Card.BadRed);
            if (o.Ok)
            {
                _cdk.Text = "";
                Settings.SaveLastAccount(_account.Text.Trim());
            }
            SetInputs();
        }

        void Work<T>(Func<T> job, Action<T> done)
        {
            _busy = true;
            SetInputs();
            ThreadPool.QueueUserWorkItem(delegate
            {
                T value = default(T);
                Exception error = null;
                try { value = job(); }
                catch (Exception ex) { error = ex; }
                if (IsDisposed) return;
                try
                {
                    BeginInvoke((Action)(() =>
                    {
                        _busy = false;
                        _redeeming = false;
                        if (error != null)
                        {
                            _readingBrowser = false;
                            Say("出错了：" + error.GetType().Name + "。请关闭窗口后重试。", Card.BadRed);
                            SetInputs();
                            return;
                        }
                        done(value);
                    }));
                }
                catch (InvalidOperationException) { } // window already gone
            });
        }

        void OnClosingConfirm(object sender, FormClosingEventArgs e)
        {
            if (!_redeeming || _readingBrowser) return;
            var r = MessageBox.Show(this, "兑换还在进行中。现在关闭的话，结果可以稍后再次提交同一 CDK 查询，不会重复扣除。\n确定关闭？",
                "Codex 充值", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning);
            if (r != DialogResult.OK) e.Cancel = true;
        }
    }

    // One window per kind. A second launch brings the first one to the front and exits.
    sealed class SingleInstance : IDisposable
    {
        readonly Mutex _mutex;
        readonly EventWaitHandle _activate;
        public readonly bool First;

        public SingleInstance(string kind)
        {
            bool created;
            _mutex = new Mutex(true, "Local\\CodexNativeAssist.Window." + kind, out created);
            First = created;
            _activate = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\CodexNativeAssist.Activate." + kind);
            if (!First) _activate.Set();
        }

        public void OnActivate(Form form)
        {
            var t = new Thread(delegate ()
            {
                while (true)
                {
                    _activate.WaitOne();
                    try
                    {
                        form.BeginInvoke((Action)(() =>
                        {
                            if (form.WindowState == FormWindowState.Minimized) form.WindowState = FormWindowState.Normal;
                            form.TopMost = true;
                            form.Activate();
                            form.TopMost = false;
                        }));
                    }
                    catch (Exception) { return; }
                }
            });
            t.IsBackground = true;
            t.Start();
        }

        public void Dispose()
        {
            if (First) { try { _mutex.ReleaseMutex(); } catch (Exception) { } }
            _mutex.Dispose();
            _activate.Dispose();
        }
    }
}
