# GeminiWatch — Gemini 可用性监控

跨平台常驻小工具（Windows / macOS / Linux）：按固定间隔探测 Gemini 相关端点，异常时弹系统通知，同时提供托盘状态与本地 Web 面板。

## 目录

- [功能](#功能) · [目录结构](#目录结构) · [快速开始](#快速开始)
- [判定规则](#判定规则) · [通知规则](#通知规则) · [配置说明](#配置说明)
- [打包](#打包) · [开机自启](#开机自启) · [平台差异](#平台差异)
- [常见问题](#常见问题) · [安全与脱敏](#安全与脱敏) · [已知限制](#已知限制)

## 功能

- **定时巡检**：默认每 60 秒探测四个目标，并行执行，单项 5 秒超时（硬截止 7 秒）
- **四项探测**：网络连通（`generate_204`）、Antigravity 后端、Gemini API、Gemini 网页版
- **异常通知**：单轮不可用即提醒，恢复时再提醒一次；故障期间不重复刷屏
- **故障诊断**：判为网络问题时自动做 DNS / TCP 443 / TLS / 代理端口 / 代理进程链路排查
- **常驻托盘**：图标按状态变色；Windows 单击/双击打开面板，macOS 点击弹菜单，菜单内可暂停、立即检测、打开配置、退出
- **Web 状态面板**：总状态徽章、四张目标卡片（状态 / 延迟 / 可用率 / P95）、近 24 小时可用率、延迟趋势图、故障事件、检测记录、清空记录（二次确认）
- **单实例**：重复启动会直接退出并提示，不会开第二个进程
- **配置热加载**：改完 `config.json` 保存即生效；文件被删会自动重建默认配置
- **端口自愈**：面板端口被占用时自动顺延尝试 `port+1 ~ port+30`

## 目录结构

```
gemini-watch/
├─ gemini_watch.py     # 主程序（探测、判定、通知、面板、托盘）
├─ panel.html          # 状态面板页面（单文件，打包时内嵌）
├─ config.json         # 默认配置模板（运行实际使用用户数据目录下的 config.json）
├─ build_exe.bat       # Windows 一键打包 exe
├─ build_mac.sh        # macOS 一键打包 .app
├─ .github/workflows/build.yml      # 云端构建三平台产物
├─ requirements.txt    # 运行依赖
├─ start_watch.bat     # Windows 源码方式启动（pythonw，无窗口）
├─ assets/app.ico      # Windows 多尺寸图标
├─ dist/               # 打包产物（不纳入版本管理）
└─ README.md
```

运行期数据按系统放在用户目录，不写程序目录：

| 系统 | 数据目录 |
|---|---|
| Windows | `%APPDATA%\GeminiWatch\` |
| macOS | `~/Library/Application Support/GeminiWatch/` |
| Linux | `~/.config/GeminiWatch/` |

可用环境变量 `GEMINI_WATCH_HOME` 覆盖该路径（便于测试或多实例）。

目录内文件：

| 文件 | 说明 |
|---|---|
| `config.json` | 实际生效的配置，删除后下次启动自动重建 |
| `gemini_watch.log` | 运行日志（含每轮判定、通知结果、故障诊断），超过 5MB 轮转 |
| `history.jsonl` | 检测历史（面板的可用率、趋势、事件都来自这里），超过 5MB 轮转 |
| `gemini_watch.lock` | 单实例锁，进程退出自动释放 |

## 快速开始

### Windows：直接运行 exe

双击 `dist\GeminiWatch.exe`，托盘出现图标后浏览器打开 <http://127.0.0.1:47131>。

### 任意平台：源码运行

```bash
python -m pip install -r requirements.txt    # Windows
python3 -m pip install -r requirements.txt   # macOS / Linux
```

`requirements.txt` 里 `winotify` 带平台标记，非 Windows 会自动跳过。

```bash
python gemini_watch.py            # 前台调试，日志同时输出到控制台
python gemini_watch.py --once     # 只检测一轮，先验证网络与判定
python gemini_watch.py --no-tray  # 不启托盘（纯后台 + 面板）
```

Windows 无窗口常驻用 `pythonw gemini_watch.py` 或双击 `start_watch.bat`；macOS / Linux 直接后台运行即可。

## 判定规则

| 探测结果 | 判定 | 说明 |
|---|---|---|
| HTTP 2xx / 3xx | 可用 | 正常响应 |
| HTTP 401 / 403 / 404 | 可用 | 只是未鉴权或路径不存在，链路是通的 |
| HTTP 429 | 降级 | 限流 / 额度用尽 |
| HTTP 5xx | 不可用 | 服务端故障（Antigravity 常见 503） |
| 超时 / DNS 失败 / TLS 失败 / 连接被拒 | 不可用 | 网络或代理问题 |

总状态取当轮四项中最差的一项，实时更新；`可用率` 卡片是近 24 小时累计值，不代表当前瞬间。

## 通知规则

| 配置项 | 默认 | 行为 |
|---|---|---|
| `fail_threshold` | 1 | 连续失败几轮报警；1 = 单轮即报，2 = 过滤抖动 |
| `recover_threshold` | 2 | 连续成功几轮才发"已恢复"（防止来回抖动弹两条） |
| `repeat_alert_minutes` | 0 | 0 = 一次故障只提醒一次；>0 = 持续故障每隔该分钟数再提醒 |

例：`⚠️ Gemini 不可用 · Gemini API、Gemini 网页不可用（未走代理）`，恢复时 `✅ Gemini 已恢复`。

是否真的发出通知会写入日志，搜索 `已发送桌面通知` / `桌面通知发送失败` 即可区分"没触发"和"被系统拦截"。

## 配置说明

配置文件：`%APPDATA%\GeminiWatch\config.json`（源码目录下的 `config.json` 只是模板，改它不影响运行中的实例）。

| 字段 | 默认 | 说明 |
|---|---|---|
| `interval_seconds` | 60 | 检测间隔（秒） |
| `timeout_seconds` | 5 | 单项目超时（秒），实际硬截止为超时 + 2 秒 |
| `proxy` | `""` | 指定代理地址；留空则按系统代理 / TUN 自动判断 |
| `log_file` / `history_file` | — | 日志与历史文件名（位于数据目录） |
| `notify.enabled` / `notify.sound` | true / true | 桌面通知总开关与提示音 |
| `diagnose.enabled` | true | 故障时是否做链路诊断 |
| `diagnose.proxy_port` | 7890 | 诊断时检查的本地代理端口 |
| `diagnose.processes` | clash、mihomo、verge… | 诊断时检查的代理进程名 |
| `panel.enabled` / `panel.host` / `panel.port` | true / 127.0.0.1 / 47131 | 状态面板监听地址；端口占用自动顺延 |
| `tray.enabled` | true | 是否启用托盘 |
| `path_check.enabled` | true | 是否判断当前流量路径（系统代理 / TUN / 直连） |
| `path_check.require_proxy` | false | 开启后，未走代理即使探测成功也判为降级 |
| `region_check.enabled` | true | 是否启用地区校验 |
| `region_check.api_key` | `""` | 填了 Gemini API key 才会走官方接口判定地区（最准）；留空则跳过 |
| `region_check.interval_minutes` | 10 | 地区校验缓存时长 |
| `targets[]` | 3 项 | `name` / `url` / `method`，可选 `expect`（期望状态码） |

说明：项目**不做 IP 归属地推断**（ipinfo / Cloudflare / Google 各家结果经常互相矛盾，误报率高），只保留"带 key 调官方接口看是否返回地区不支持"这一种准确方式。

启动时会自动补一个 `网络连通` 目标（`http://www.gstatic.com/generate_204`，期望 204），用于区分"自己的网断了"和"Gemini 挂了"。

## 打包

> PyInstaller 不支持交叉编译：每个平台的产物必须在对应系统上构建。没有 Mac 时用下面的云端构建。

### 云端构建（三平台）

仓库内置 [`.github/workflows/build.yml`](.github/workflows/build.yml)：推送到 `main` / `master`、打 `v*` 标签，或在 Actions 页面手动 `Run workflow`，会并行构建 Windows / macOS / Linux 三个平台。

使用前提：**Git 仓库根就是这个 `gemini-watch` 目录**。若把仓库建在上级目录，需要在 workflow 里补 `defaults.run.working-directory: gemini-watch` 并调整 checkout 路径。

产物在运行详情页底部 **Artifacts**：

| Artifact | 内容 | 说明 |
|---|---|---|
| `GeminiWatch-windows-x64` | `GeminiWatch.exe` | 单文件、无控制台，双击即用 |
| `GeminiWatch-windows-arm64` | `GeminiWatch.exe` | Windows 11 ARM（实验性，见下） |
| `GeminiWatch-macOS-arm64` | `GeminiWatch.app`（zip） | Apple Silicon（M1/M2/M3/M4），`ditto` 打包保留权限 |
| `GeminiWatch-macOS-x64` | `GeminiWatch.app`（zip） | Intel 芯片 Mac |
| `GeminiWatch-linux-x64` | `GeminiWatch`（tar.gz） | x86_64 单文件，解压后 `chmod +x` 直接运行 |

Windows 与 macOS 都用矩阵分别构建两个架构，按芯片取对应包即可。

Windows 的 arm64 组合跑在 `windows-11-arm` runner 上，依赖 PyInstaller 的 ARM64 bootloader，属于实验性支持：该组合允许失败（`continue-on-error`），不会影响 x64 产物。如果不产出，x64 版在 Windows 11 ARM 上也能通过系统模拟正常运行，只是性能与功耗略差。

补充说明：

- macOS 产物未签名，首次打开会被 Gatekeeper 拦下：`xattr -dr com.apple.quarantine GeminiWatch.app`
- 若 `macos-15-intel` 标签不可用，Intel 版可改用自建 Mac 构建，或改走 universal2（需 universal2 版 Python 且依赖从源码编译）
- Linux 版托盘依赖图形桌面（AppIndicator），无桌面时加 `--no-tray`
- 构建在公开 runner 上进行，产物不含任何本机配置；首次运行会在目标机器上生成默认配置
- Windows 的 CI 构建复用 `build_exe.bat`，脚本检测到 `CI` 环境变量时会跳过 `pause`

### 发布新版本

打一个 `v` 开头的标签并推送，会自动构建并创建 GitHub Release：

```bash
git tag v1.0.1
git push origin v1.0.1
```

流程是：三个平台并行构建 → 汇总产物 → 创建同名 Release 并上传附件（若该标签已有 Release，则覆盖上传）。附件名会自动带版本号，例如 `GeminiWatch-v1.0.1-windows-x64.exe`。

普通 push 只生成 Actions Artifacts，不会发 Release。某个架构构建失败时，其余产物仍会照常发布。

### Windows

双击 `build_exe.bat`，产物为 `dist\GeminiWatch.exe`（单文件、无控制台）。脚本依次完成：

1. 检查 Python，以及 `gemini_watch.py` / `panel.html` / 图标是否齐全
2. 安装或更新 PyInstaller 与运行依赖
3. 结束正在运行的 `GeminiWatch.exe`（否则旧文件被占用会导致构建失败）
4. 清理旧构建并打包
5. 校验产物，清理 `build/` 与 `*.spec`

成功后打印产物路径、数据目录与面板地址。同样地，产物架构跟随本机 Python：x64 系统出 x64，Windows on ARM 上原生 Python 出 arm64。

手动命令等价于：

```powershell
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name GeminiWatch `
  --icon "assets\app.ico" --add-data "panel.html;." `
  --collect-submodules pystray --collect-submodules winotify gemini_watch.py
```

打包前请先退出正在运行的实例，否则会因文件占用报 `WinError 5`。

### macOS

PyInstaller **不支持交叉编译**，必须在 macOS 上构建。执行：

```bash
chmod +x build_mac.sh
./build_mac.sh
```

产物为 `dist/GeminiWatch.app`。脚本会安装依赖、结束运行中的实例、用 `iconutil` 从 `assets/app.ico` 生成 `.icns`，再打包并校验产物。

产物架构跟随本机 Python：Apple Silicon 上得到 arm64，Intel Mac 上得到 x64，两者不能互换。想在一个平台上同时产出双架构，用上面的 CI 矩阵，或改用 universal2 方案。

手动命令等价于：

```bash
python3 -m PyInstaller --noconfirm --clean --windowed --name GeminiWatch \
  --add-data "panel.html:." --collect-submodules pystray \
  --icon assets/app.icns gemini_watch.py
```

macOS 上 `--add-data` 用冒号分隔，`--windowed` 生成 `.app`，建议用默认的 `--onedir` 而不是 `--onefile`（后者每次启动都要解压，冷启动明显变慢）。

未签名的 `.app` 会被 Gatekeeper 拦下，首次运行可执行 `xattr -dr com.apple.quarantine GeminiWatch.app`，或使用开发者证书签名 + 公证后再分发。

## 开机自启

**Windows**

1. `Win + R` 输入 `shell:startup` 回车
2. 把 `dist\GeminiWatch.exe` 的快捷方式放进该目录

配置固定在 `%APPDATA%\GeminiWatch\config.json`，快捷方式无需带参数。

**macOS**

在 `~/Library/LaunchAgents/com.geminiwatch.plist` 写入：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.geminiwatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/绝对路径/gemini-watch/gemini_watch.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.geminiwatch.plist
```

`python3` 路径以 `which python3` 为准；如需菜单栏图标，保留托盘（不要加 `--no-tray`），且必须在图形登录会话中运行。

**Linux**

用 `systemd --user` 建一个 service 指向 `python3 gemini_watch.py`，设置 `Restart=on-failure` 即可。

## 平台差异

| 能力 | Windows | macOS | Linux |
|---|---|---|---|
| 桌面通知 | winotify Toast | `osascript` 通知中心 | `notify-send` |
| 系统代理检测 | 注册表 `ProxyEnable` | `scutil --proxy` | 不做判断 |
| TUN 网卡检测 | `netsh interface` | `ifconfig` 中带 IPv4 的 `utun*` | 不启用 |
| 单实例锁 | `msvcrt.locking` | `fcntl.flock` | `fcntl.flock` |
| 托盘 | pystray（单击打开面板） | pystray（点击弹菜单） | pystray（需桌面环境支持） |

Linux 分支为同一套兼容实现，未做完整实测；macOS 分支已通过模拟数据验证路径、代理与 TUN 解析逻辑。

## 常见问题

**收不到通知？**
先看日志有没有 `已发送桌面通知`。有的话是显示层被拦截，逐项检查：`Win + N` 通知中心是否有记录、设置 → 系统 → 通知里本应用是否开启、"请勿打扰"是否关闭、是否处于全屏应用下。

**面板打不开？**
看日志里的实际地址（端口被占用时会自动顺延，日志会打印 `已自动切换到 xxxxx`）。也可用托盘菜单的"打开状态面板"。

**macOS 提示"无法打开，因为无法验证开发者"？**
执行 `xattr -dr com.apple.quarantine dist/GeminiWatch.app`，或到"系统设置 → 隐私与安全性"点"仍要打开"。

**macOS 菜单栏没有图标？**
确认是在图形登录会话中运行（不是 SSH 远程），并检查日志里是否有托盘初始化失败的 `WARN`；`pystray` 在 macOS 上依赖的 pyobjc 会随 pip 安装一起装上。

**开了代理还是提示不可用？**
看面板右上角的"路径"。如果是"代理端口未接管"，说明代理软件在跑但没接管系统流量（既没开系统代理也没开 TUN），此时探测会走直连而超时，属于真实不可用。

**改了配置没生效？**
保存后会在下一轮自动重载；端口变化会重启面板监听。若配置文件语法错误，程序会沿用旧配置并在日志里记录。

**怎么彻底退出？**
托盘菜单"退出"会停止检测并释放面板端口；也可以在任务管理器 / 活动监视器里结束进程。单实例锁由操作系统释放，不会残留。

## 安全与脱敏

- 仓库内所有文件**不含**密钥、账号、密码、内网/公网 IP、本机绝对路径与用户名，可直接公开
- `region_check.api_key` 默认留空；若自行填写，会以**明文**保存在本机数据目录的 `config.json`，请勿提交或分享该文件
- 日志、检测历史、锁文件均由 `.gitignore` 排除，且只保存在本机数据目录
- 状态面板仅监听 `127.0.0.1`，不对外网开放

## 已知限制

- 桌面通知依赖系统通知中心，被免打扰/专注模式拦截时只进通知中心
- macOS 的通知由 `osascript` 发出，系统里显示为"脚本编辑器"；如需自定义应用名可改用 `terminal-notifier`
- 地区判定需要自备 Gemini API key，未配置时不做地区结论
- 不支持交叉编译：Windows exe 需在 Windows 打包，macOS `.app` 需在 macOS 打包
