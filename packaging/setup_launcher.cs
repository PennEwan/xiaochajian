using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Windows.Forms;

// Codex 助手包 · 安装包启动器
//
// 为什么需要它：Inno Setup 在启动时会在 %TEMP% 下创建一个“受保护”的临时目录（带受限 DACL）。
// 在云桌面 / VHD 系统盘上（本机就是：C: 是「云桌面系统盘」），这个带安全描述符的目录创建会被
// 拒绝，安装程序在向导出现之前就弹「错误 5: 拒绝访问」，用户只会看到“无法安装到 C 盘”。
// 本启动器挑一个**真的能创建受保护目录**的位置，把它作为 /TMP 传给真正的安装程序：
//   1) 系统临时目录 —— 正常机器的默认选择；
//   2) 启动器自身所在目录下的 .setup-tmp —— 安装包放在别的盘时（云桌面常见）用这块盘；
//   3) %LOCALAPPDATA%\CodexCompanionSetup；
//   4) %ProgramData%\CodexCompanionSetup。
// 探测方式与 Inno 一致：创建目录 + 给它设置一个受限 DACL + 写一个文件。
// 参数原样透传给真正的安装程序（/SILENT /VERYSILENT /LOG=… /DIR=… 都能用）。
internal static class SetupLauncher
{
    private const string PayloadResource = "payload.exe";

    private static string _logPath;

    private static void Log(string message)
    {
        try
        {
            if (_logPath != null) File.AppendAllText(_logPath, DateTime.Now.ToString("HH:mm:ss.fff") + " " + message + Environment.NewLine);
        }
        catch (Exception) { }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        string self = Assembly.GetExecutingAssembly().Location;
        string launchDir = Path.GetDirectoryName(self) ?? ".";
        _logPath = Path.Combine(launchDir, "codex-companion-setup.log");
        try
        {
            return Run(args, launchDir);
        }
        catch (Exception ex)
        {
            Log("FATAL " + ex);
            MessageBox.Show("安装程序启动失败：" + ex.Message + "\n\n详细信息见：" + _logPath,
                "Codex 助手包", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 9;
        }
    }

    private static int Run(string[] args, string launchDir)
    {

        string temp;
        try
        {
            temp = PickTemp(launchDir);
            Log("temp=" + temp);
        }
        catch (Exception ex)
        {
            MessageBox.Show("无法准备安装所需的临时目录：" + ex.Message, "Codex 助手包",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }

        string payload = Path.Combine(temp, "CodexCompanion-Setup-payload-" + Guid.NewGuid().ToString("N") + ".exe");
        try
        {
            Extract(payload);
        }
        catch (Exception ex)
        {
            MessageBox.Show("无法解出安装程序：" + ex.Message, "Codex 助手包",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 3;
        }

        try
        {
            // Hand the temp directory over through the environment, not /TMP=: Inno does not accept
            // a quoted /TMP value, and a user name with a space would need quotes.
            var start = new ProcessStartInfo(payload)
            {
                UseShellExecute = false,
                WorkingDirectory = temp,
            };
            start.EnvironmentVariables["TEMP"] = temp;
            start.EnvironmentVariables["TMP"] = temp;
            start.Arguments = string.Join(" ", args);
            Log("start " + payload + " " + start.Arguments);
            using (var proc = Process.Start(start))
            {
                if (proc == null) return 4;
                proc.WaitForExit();
                Log("exit " + proc.ExitCode);
                return proc.ExitCode;
            }
        }
        finally
        {
            TryDelete(payload);
        }
    }

    private static string PickTemp(string launchDir)
    {
        string systemTemp = Path.GetTempPath().TrimEnd('\\');
        string launchRoot = Path.GetPathRoot(launchDir) ?? "";
        string systemRoot = Path.GetPathRoot(systemTemp) ?? "";
        string ownTemp = Path.Combine(launchDir, ".setup-tmp");
        string localTemp = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "CodexCompanionSetup");
        string programData = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "CodexCompanionSetup");

        // 安装包放在另一块盘上时（云桌面/VHD 常见），优先用那块盘：系统盘的“受保护目录”创建
        // 可能被拒，而用户自己的数据盘通常没这个限制。
        string[] candidates = launchRoot.Length > 0 && !string.Equals(launchRoot, systemRoot, StringComparison.OrdinalIgnoreCase)
            ? new[] { ownTemp, systemTemp, localTemp, programData }
            : new[] { systemTemp, ownTemp, localTemp, programData };

        string lastError = null;
        foreach (string candidate in candidates)
        {
            try
            {
                bool ok = CanCreateProtected(candidate);
                Log("probe " + candidate + " -> " + ok);
                if (ok) return candidate;
            }
            catch (Exception ex)
            {
                lastError = ex.Message;
            }
        }
        throw new IOException(lastError ?? "没有可用的临时目录");
    }

    /// <summary>与 Inno 的做法一致：建目录 → 设置受限 DACL → 写文件 → 清理。</summary>
    private static bool CanCreateProtected(string root)
    {
        string probe = Path.Combine(root, "is-probe-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".tmp");
        try
        {
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            var me = WindowsIdentity.GetCurrent();
            if (me != null && me.User != null)
            {
                security.AddAccessRule(new FileSystemAccessRule(me.User, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
            }
            var info = Directory.CreateDirectory(probe, security);
            info.SetAccessControl(security);
            string file = Path.Combine(probe, "probe.txt");
            File.WriteAllText(file, "ok");
            return true;
        }
        catch (Exception)
        {
            return false;
        }
        finally
        {
            TryDelete(probe);
        }
    }

    private static void Extract(string target)
    {
        using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(PayloadResource))
        {
            if (stream == null) throw new InvalidOperationException("安装包内容缺失");
            using (var file = new FileStream(target, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                stream.CopyTo(file);
            }
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            if (Directory.Exists(path)) Directory.Delete(path, true);
            else if (File.Exists(path)) File.Delete(path);
        }
        catch (Exception)
        {
            // 清理失败没有关系：它就是一个临时文件
        }
    }
}
