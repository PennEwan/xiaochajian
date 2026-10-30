using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace CodexNativeAssist
{
    // The look of the Codex 小助手 quota card (port of plugins/codex-cdk-recharge/lib/cdkr/paint.py):
    // light aurora over #edf3f8, ink text, pill chips and a blue pill button. Sizes are 96-DPI pixels.
    static class Card
    {
        public const int Width = 340, Radius = 30, PadX = 26, PadTop = 22, PadBottom = 20, MaxHeight = 560;

        public static readonly Color Bg = Color.FromArgb(237, 243, 248);
        public static readonly Color Ink = Color.FromArgb(23, 25, 31);
        public static readonly Color Muted = Tint(Ink, .68), Faint = Tint(Ink, .45);
        public static readonly Color PinBlue = Color.FromArgb(90, 144, 214), NoteBlue = Color.FromArgb(47, 98, 168);
        public static readonly Color OkGreen = Color.FromArgb(47, 158, 107), BadRed = Color.FromArgb(196, 64, 52);
        public static readonly Color StatusOk = Color.FromArgb(99, 245, 140), StatusStale = Color.FromArgb(143, 144, 148);
        public static readonly Color ChipGrey = Color.FromArgb(120, 130, 145);
        public static readonly Color WellBg = Color.FromArgb(251, 253, 255), WellEdge = Tint(ChipGrey, .38);
        public static readonly Color Blue = Color.FromArgb(0, 96, 223), BlueHot = Color.FromArgb(0, 80, 189), Off = Tint(ChipGrey, .45);
        public const string Family = "Microsoft YaHei UI";

        public static Color Mix(Color a, Color b, double t)
        {
            return Color.FromArgb((int)Math.Round(a.R + (b.R - a.R) * t), (int)Math.Round(a.G + (b.G - a.G) * t),
                (int)Math.Round(a.B + (b.B - a.B) * t));
        }

        // `c` at `alpha` opacity over the card background.
        public static Color Tint(Color c, double alpha)
        {
            return Mix(Color.FromArgb(237, 243, 248), c, alpha);
        }

        // The "healthy" aurora (小助手 ball_render.aurora): a 145° linear gradient with three radial glows,
        // laid over the card background at 42 %. `scale` = device pixels per 96-DPI pixel.
        public static Bitmap Aurora(int width, int height, float scale)
        {
            double[] cool = { 185, 213, 238 }, glow = { 223, 244, 229 }, warm = { 199, 221, 242 };
            double[][] stopColors = { cool, new double[] { 199, 201, 209 }, warm, new double[] { 255, 195, 124 } };
            double[] stops = { 0, .47, .83, 1 };
            const double inset = .39, opacity = .42;
            double boxW = width * (1 + 2 * inset), boxH = height * (1 + 2 * inset);
            double angle = 145 * Math.PI / 180, sa = Math.Sin(angle), ca = Math.Cos(angle);
            double reach = Math.Sqrt(boxW * boxW + boxH * boxH) / Math.Sqrt(2);
            double length = Math.Abs(boxW * sa) + Math.Abs(boxH * ca);
            // centre (box fractions), solid radius, fade radius (fractions of reach), tone, strength
            var blobs = new[]
            {
                new { X = .52 * boxW, Y = .12 * boxH, Solid = .22, Fade = .52, Tone = cool, Strength = .9 },
                new { X = .82 * boxW, Y = .82 * boxH, Solid = .14, Fade = .34, Tone = warm, Strength = .64 },
                new { X = .28 * boxW, Y = .68 * boxH, Solid = .18, Fade = .43, Tone = glow, Strength = .78 },
            };
            int pw = (int)Math.Ceiling(width * scale), ph = (int)Math.Ceiling(height * scale);
            var bmp = new Bitmap(pw, ph, PixelFormat.Format32bppRgb);
            var data = bmp.LockBits(new Rectangle(0, 0, pw, ph), ImageLockMode.WriteOnly, PixelFormat.Format32bppRgb);
            var row = new int[pw];
            var color = new double[3];
            try
            {
                for (int j = 0; j < ph; j++)
                {
                    double y = (j + .5) / scale + inset * height;
                    for (int i = 0; i < pw; i++)
                    {
                        double x = (i + .5) / scale + inset * width;
                        double t = ((x - boxW / 2) * sa - (y - boxH / 2) * ca) / length + .5;
                        t = Math.Max(0, Math.Min(1, t));
                        for (int s = 1; s < stops.Length; s++)
                        {
                            if (t > stops[s] && s < stops.Length - 1) continue;
                            double f = (t - stops[s - 1]) / (stops[s] - stops[s - 1]);
                            for (int k = 0; k < 3; k++) color[k] = stopColors[s - 1][k] + (stopColors[s][k] - stopColors[s - 1][k]) * f;
                            break;
                        }
                        foreach (var b in blobs)
                        {
                            double dx = x - b.X, dy = y - b.Y, d = Math.Sqrt(dx * dx + dy * dy) / reach;
                            double a = d <= b.Solid ? b.Strength : b.Strength * Math.Max(0, 1 - (d - b.Solid) / (b.Fade - b.Solid));
                            if (a > 0) for (int k = 0; k < 3; k++) color[k] += (b.Tone[k] - color[k]) * a;
                        }
                        int r = (int)Math.Round(237 + (color[0] - 237) * opacity);
                        int g = (int)Math.Round(243 + (color[1] - 243) * opacity);
                        int bl = (int)Math.Round(248 + (color[2] - 248) * opacity);
                        row[i] = (r << 16) | (g << 8) | bl;
                    }
                    Marshal.Copy(row, 0, data.Scan0 + j * data.Stride, pw);
                }
            }
            finally
            {
                bmp.UnlockBits(data);
            }
            return bmp;
        }

        public static GraphicsPath RoundRect(RectangleF r, float radius)
        {
            radius = Math.Max(0, Math.Min(radius, Math.Min(r.Width, r.Height) / 2));
            var p = new GraphicsPath();
            if (radius <= 0)
            {
                p.AddRectangle(r);
                return p;
            }
            float d = radius * 2;
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }

        public static void FillRound(Graphics g, Color fill, RectangleF r, float radius, Color? edge = null)
        {
            var mode = g.SmoothingMode;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = RoundRect(r, radius))
            using (var brush = new SolidBrush(fill))
            {
                g.FillPath(brush, path);
                if (edge.HasValue)
                    using (var pen = new Pen(edge.Value, 1f)) g.DrawPath(pen, path);
            }
            g.SmoothingMode = mode;
        }

        // The small pill the 小助手 card uses for tags (ball_render._chip). Returns its left edge.
        public static float Chip(Graphics g, string text, Font font, float right, float middle, float scale)
        {
            if (string.IsNullOrEmpty(text)) return right;
            var size = TextRenderer.MeasureText(g, text, font, Size.Empty, TextFormatFlags.NoPadding);
            float w = size.Width + 16 * scale, h = 19 * scale;
            var box = new RectangleF(right - w, middle - h / 2, w, h);
            FillRound(g, Tint(PinBlue, .28), box, h / 2);
            TextRenderer.DrawText(g, text, font, Rectangle.Round(box), NoteBlue,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
            return box.X;
        }
    }

    // A real Win32 button (keyboard, BM_CLICK and UI Automation keep working) drawn as a 小助手 pill.
    sealed class PillButton : Button
    {
        public Color Fill = Card.Blue, Hot = Card.BlueHot, InkColor = Color.White;
        public string Glyph; // drawn instead of Text (Text stays the accessible name)
        public Image Backdrop; // the parent's background image, drawn behind the pill's rounded corners
        bool _hover;

        public PillButton()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.ResizeRedraw, true);
            BackColor = Card.Bg;
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            Cursor = Cursors.Hand;
        }

        protected override bool ShowFocusCues { get { return false; } }

        protected override void OnMouseEnter(EventArgs e) { _hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { _hover = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
        protected override void OnTextChanged(EventArgs e) { Invalidate(); base.OnTextChanged(e); }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            if (Backdrop == null) { base.OnPaintBackground(e); return; }
            e.Graphics.DrawImage(Backdrop, new Rectangle(0, 0, Width, Height), new Rectangle(Left, Top, Width, Height), GraphicsUnit.Pixel);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            OnPaintBackground(e);
            Color fill = !Enabled ? Card.Off : _hover ? Hot : Fill;
            Color ink = Enabled ? InkColor : Card.Mix(Card.Off, InkColor, .75);
            var r = new RectangleF(0, 0, Width - 1, Height - 1);
            Card.FillRound(e.Graphics, fill, r, r.Height / 2);
            TextRenderer.DrawText(e.Graphics, Glyph ?? Text, Font, new Rectangle(0, 0, Width, Height), ink, fill,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
        }
    }
}
