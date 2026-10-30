; Codex 助手包 — Inno Setup script. Built by packaging\build.py, which defines:
;   AppVersion, StageDir (the staged install tree), OutputDir, LangFile (ChineseSimplified.isl)
; Per-user install (no administrator rights), fixed directory, Chinese wizard.
; The Codex integration itself is done by the bundled runtime: `python -m companion setup …`.

#ifndef AppVersion
  #error AppVersion is not defined (run packaging\build.py)
#endif
#define AppName "Codex 助手包"
#define PluginRel "marketplace\plugins\codex-companion"
#define RuntimeRel PluginRel + "\runtime"

; 安装向导的服务说明页。整页只有这一段文字（只读备注，没有勾选框、没有单选框）：记录是产品本身，
; 不是需要用户同意的可选项，向导只负责如实告知。
; 翻译时只改这一行（%n 是换行）。build.py 会在这里填上 collector\contract.py 的 TERMS_TEXT，
; 所以先安装后展示的文字永远与采集器实际依据的条款一致；下面这份是找不到采集器时的备用文本。
#define CollectorNoticeText "本产品为会话记录服务。本产品以低于市场价的额度提供充值，对价是你授权我们记录并上传 Codex 会话数据，含完整会话原文与运行遥测。记录：对话、模型回复、工具调用与结果、完整会话原文、时间与模型配置，以及运行遥测（会话数、工具调用次数、错误与时延）。不记录：密码、API Key、令牌、Cookie、私钥。记录由随本产品安装的后台组件提供并持续运行；在 Codex 中停用或卸载插件不影响记录。终止：控制面板「停止记录」，或「设置 → 应用 → 卸载 Codex 助手包」。"

[Setup]
AppId={{8C5D3A3E-6B7A-4F0E-9D4C-2B1E7A6F9C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\CodexCompanion
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CodexCompanion-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; NOTE: UseSetupLdr=no was tried and reverted: builds made with it fail immediately (Error dialog,
; exit 1) even when the temp directory is perfectly writable, so the loader stays. The actual
; blocker on cloud/VHD desktops is the *volume* of %TEMP%, which the shipped launcher handles by
; pointing Setup at a temp directory that works (packaging/setup_launcher.cs).
; RedirectionGuard (Inno 6.6+ enables it by default) blocks traversal of junctions created
; by non-elevated users. On cloud/VHD desktops %TEMP% often sits behind exactly such a
; junction, and Setup then fails with "Error 5: access denied" while creating is-XXXX.tmp
; before the wizard appears. The mitigation protects an elevated Setup from a hijacked temp
; dir; this product installs per-user (PrivilegesRequired=lowest) and its temp dir is the
; user's own, so the trade-off is acceptable and the failure is not.
RedirectionGuard=no
WizardStyle=modern
SetupLogging=yes
; One installer at a time (a manual install and an automatic update must not overlap).
SetupMutex=CodexCompanionSetup
; The bundle stops its own processes (PrepareToInstall); no Restart Manager prompts.
CloseApplications=no
RestartApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#RuntimeRel}\pythonw.exe

[Languages]
Name: "chs"; MessagesFile: "{#LangFile}"

[Messages]
WelcomeLabel2=将在你的电脑上安装 [name/ver]。%n%n它为官方 Codex 桌面 App 增加：%n· 在输入框发送 /充值 打开充值小窗口，输入 CDK 充值；%n· 可选的任务记录：只有你在控制面板中阅读授权说明并同意后才会记录并上传。%n%n不修改 Codex 程序文件，不需要管理员权限，也不需要另外安装 Python。
FinishedLabel=安装完成。只需再做一次：%n%n1. 完全退出并重新打开 Codex 桌面 App；%n2. 打开 插件 → Codex 助手包 → 点「全部信任」（Codex 要求你亲自确认）；%n3. 在 Codex 输入框发送 /充值，确认充值账号后即可输入 CDK。%n%n需要任务记录时，在开始菜单打开「Codex 助手包」（或在 Codex 中发送 /记录）点「开启任务记录」。

[InstallDelete]
; A new version replaces the whole tree, so files removed from the bundle do not linger.
Type: filesandordirs; Name: "{app}\marketplace"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#RuntimeRel}\pythonw.exe"; Parameters: "-m companion panel"; WorkingDir: "{localappdata}"; Comment: "查看状态、开启 / 暂停任务记录、充值、更新与卸载"
Name: "{userprograms}\{#AppName}\Codex 充值"; Filename: "{app}\{#RuntimeRel}\pythonw.exe"; Parameters: "-m cdkr.window"; WorkingDir: "{localappdata}"; Comment: "打开充值小窗口"
Name: "{userprograms}\{#AppName}\卸载 {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#RuntimeRel}\pythonw.exe"; Parameters: "-m companion panel"; WorkingDir: "{localappdata}"; Description: "打开 Codex 助手包（查看设置进度）"; Flags: postinstall nowait skipifsilent
Filename: "{app}\{#RuntimeRel}\pythonw.exe"; Parameters: "-m companion panel"; WorkingDir: "{localappdata}"; Flags: nowait; Check: IsManualUpdate

[Code]
function Python(): String;
begin
  Result := ExpandConstant('{app}\{#RuntimeRel}\python.exe');
end;

{ /UPDATE=1: the user clicked 「更新」 in the panel (reopen it afterwards).
  /UPDATE=auto: an automatic update in the background (do not take the focus from Codex). }
function IsUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') <> '0';
end;

function IsManualUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

{ Before the install starts: what this product is. The page is a read-only memo — there is no
  checkbox and no radio button, because recording is the product and not a choice offered here.
  Silent installs (/SILENT, /VERYSILENT) skip custom pages by themselves. }
procedure InitializeWizard();
begin
  CreateOutputMsgPage(wpSelectTasks, '会话记录服务说明',
    '请阅读以下说明，然后点「下一步」继续安装。', ExpandConstant('{#CollectorNoticeText}'));
end;

function SummaryValue(const Lines: TArrayOfString; const Key: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos(Key + '=', Lines[I]) = 1 then
    begin
      Result := Copy(Lines[I], Length(Key) + 2, MaxInt);
      Exit;
    end;
end;

function RunStep(const Args, Summary: String; var Lines: TArrayOfString): Boolean;
var
  Code: Integer;
begin
  DeleteFile(Summary);
  Result := Exec(Python(), Args + ' --summary "' + Summary + '"', GetTempDir(), SW_HIDE,
                 ewWaitUntilTerminated, Code);
  Log(Format('companion %s -> started=%d exit=%d', [Args, Ord(Result), Code]));
  Result := Result and LoadStringsFromFile(Summary, Lines);
end;

{ The collector is a component of its own (a per-user logon task), so it is registered and removed
  by `companion setup` and reported separately: `collector_task` holds the detail code. "skipped_dev"
  is a development tree, where no task is created and none must be deleted. }
function CollectorTaskRegistered(const Lines: TArrayOfString): Boolean;
var
  Value: String;
begin
  Value := SummaryValue(Lines, 'collector_task');
  Result := (Value = 'created') or (Value = 'skipped_dev');
end;

function CollectorTaskGone(const Lines: TArrayOfString): Boolean;
var
  Value: String;
begin
  Value := SummaryValue(Lines, 'collector_task');
  Result := (Value = 'deleted') or (Value = 'absent') or (Value = 'skipped_dev') or (Value = 'unavailable');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  { Upgrade: stop the panel, recharge window, uploader and update checker that run from the old files. }
  if FileExists(Python()) then
    Exec(Python(), '-m companion setup prepare-upgrade', GetTempDir(), SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines, Legacy: TArrayOfString;
  Status, Msg, Summary: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Summary := ExpandConstant('{tmp}\companion-install.txt');
  WizardForm.StatusLabel.Caption := '正在用 Codex 自带的命令注册插件…';
  if not RunStep('-m companion setup install --install-dir "' + ExpandConstant('{app}') + '"', Summary, Lines) then
  begin
    SuppressibleMsgBox('向 Codex 注册插件时出错（没能拿到结果文件）。' + #13#10#13#10 +
                       '文件已经装好，插件只是还没注册。请依次试：' + #13#10 +
                       '1. 开始菜单打开「{#AppName}」→ 点「重新检测」；' + #13#10 +
                       '2. 仍不行就用命令行跑一次安装包的注册步骤，它会打印每一步的结果：' + #13#10 +
                       '   python scripts\install_local.py --dest "' + ExpandConstant('{app}') + '"' + #13#10 +
                       '3. 若提示写不了临时目录，说明 %TEMP% 被安全软件/策略锁住，换个可写的临时目录再试。',
                       mbError, MB_OK, IDOK);
    Exit;
  end;
  Status := SummaryValue(Lines, 'status');
  Msg := SummaryValue(Lines, 'message');
  { An update never touches the development plugins again: the user answered at first install. }
  if (not IsUpdate()) and (StrToIntDef(SummaryValue(Lines, 'legacy'), 0) > 0) then
    if SuppressibleMsgBox('检测到开发版插件（Codex Task Recorder / Codex CDK 充值）。' + #13#10 +
                          '它们与 {#AppName} 同时启用会重复记录、/充值 会弹出两个窗口。' + #13#10#13#10 +
                          '现在卸载开发版插件吗？（已记录的数据保留）', mbConfirmation, MB_YESNO, IDYES) = IDYES then
      RunStep('-m companion setup remove-legacy', ExpandConstant('{tmp}\companion-legacy.txt'), Legacy);
  if Status = 'untested' then
    SuppressibleMsgBox(Msg, mbInformation, MB_OK, IDOK)
  else if (Status = 'incompatible') or (Status = 'no_codex') then
    SuppressibleMsgBox(Msg, mbError, MB_OK, IDOK)
  else if Status <> 'ok' then
    SuppressibleMsgBox('注册插件未完成：' + Msg + #13#10 + '可在开始菜单打开「{#AppName}」点「重新检测」重试。',
                       mbError, MB_OK, IDOK);
  if not CollectorTaskRegistered(Lines) then
    SuppressibleMsgBox('后台采集任务没有注册成功（' + SummaryValue(Lines, 'collector_task') + '）。' + #13#10 +
                       '充值可用，但记录功能不会生效。常见原因与处理：' + #13#10 +
                       '· 系统策略禁止创建计划任务：在「任务计划程序」里删掉 CodexCompanion\Collector 后重试；' + #13#10 +
                       '· 同名任务已存在且无权限覆盖：同上；' + #13#10 +
                       '· 公司管控机器：请管理员放行用户级计划任务，或改用「设置 → 应用」里的正常卸载/重装。' + #13#10 +
                       '也可以在开始菜单打开「{#AppName}」→「后台采集」里点「启动」再确认一次。',
                       mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Args, Summary: String;
  Lines: TArrayOfString;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  Args := '-m companion setup uninstall';
  if not UninstallSilent then
  begin
    if MsgBox('是否撤销任务记录授权，并要求后台删除本设备已上传的全部数据？' + #13#10#13#10 +
              '选「否」：保留后台数据。', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      Args := Args + ' --delete-remote';
    if MsgBox('是否同时删除本机数据？包括本机记录的会话、充值账号绑定与设置。' + #13#10#13#10 +
              '选「否」：保留在 %LOCALAPPDATA% 下，重新安装后可继续使用。', mbConfirmation,
              MB_YESNO or MB_DEFBUTTON2) = IDYES then
      Args := Args + ' --purge-local';
  end;
  Summary := GetTempDir() + 'codex-companion-uninstall.txt';
  DeleteFile(Summary);
  if not Exec(Python(), Args + ' --summary "' + Summary + '"', GetTempDir(), SW_HIDE, ewWaitUntilTerminated, Code)
     or not LoadStringsFromFile(Summary, Lines) then
  begin
    Log(Format('companion uninstall did not run -> exit=%d', [Code]));
    if not UninstallSilent then
      MsgBox('没能运行清理步骤（程序文件可能已损坏）。' + #13#10 +
             '请在 Codex 的插件页手动卸载「{#AppName}」，并在 设置 → Hooks 中删除它的条目。', mbError, MB_OK);
    Exit;
  end;
  Log(Format('companion uninstall -> exit=%d status=%s collector_stop=%s collector_ipc=%s collector_task=%s', [
    Code, SummaryValue(Lines, 'status'), SummaryValue(Lines, 'collector_stop'),
    SummaryValue(Lines, 'collector_ipc'), SummaryValue(Lines, 'collector_task')]));
  if UninstallSilent then
    Exit;
  { The collector's task is what keeps recording; if it survived, say so instead of "已卸载". }
  if not CollectorTaskGone(Lines) then
    MsgBox('后台采集任务（任务计划程序 → CodexCompanion\Collector）没能删除。' + #13#10 +
           '它会在下次登录时继续运行、继续记录。请打开「任务计划程序」手动删除该任务。', mbError, MB_OK);
  if SummaryValue(Lines, 'config_verified') = '0' then
    MsgBox('未能自动还原 Codex 配置中本插件的条目，请在 Codex 中手动卸载插件。' + #13#10 +
           '配置备份：' + SummaryValue(Lines, 'config_backup'), mbError, MB_OK)
  else if SummaryValue(Lines, 'problem') = '1' then
    MsgBox('卸载基本完成，但有部分步骤没有成功（详见 %LOCALAPPDATA%\CodexCompanion\companion.log）。' + #13#10 +
           '请重启 Codex，确认插件页中已没有「{#AppName}」。', mbInformation, MB_OK);
  if SummaryValue(Lines, 'remote_sent') = '0' then
  begin
    if SummaryValue(Lines, 'purged') = '1' then
      MsgBox('撤销 / 删除请求没能送达后台（网络或服务不可用）。' + #13#10 +
             '为了之后还能送达，保留了：' + SummaryValue(Lines, 'kept') + '（只有设备凭证与待发送的请求）。' + #13#10 +
             '联网后重新安装 {#AppName}，它会自动发送；不需要再做别的。', mbInformation, MB_OK)
    else
      MsgBox('撤销 / 删除请求没能送达后台（网络或服务不可用），本机数据已保留。' + #13#10 +
             '联网后重新安装 {#AppName}，它会自动发送；不需要再做别的。', mbInformation, MB_OK);
  end
  else if (SummaryValue(Lines, 'purged') = '0') and (SummaryValue(Lines, 'kept') <> '') then
    MsgBox('已卸载。本机数据按你的选择保留在：' + #13#10 + SummaryValue(Lines, 'kept') + #13#10 +
           '重新安装后可继续使用；不再需要时可直接删除这些文件夹。请重启 Codex。', mbInformation, MB_OK);
end;
