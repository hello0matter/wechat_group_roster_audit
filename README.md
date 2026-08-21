# wechat_group_roster_audit

## 主要原理

通过 `pywinauto` 对当前已登录桌面微信做只读 UI 探测，并在明确指定时导出群昵称。
本项目不读取、不保存微信号、WXID、手机号或其他账号标识。

## 使用说明

安装固定版本依赖：

```powershell
& "D:\Program Files\Python\Python311\python.exe" -m pip install -r requirements.txt
```

1. 启动并登录微信。
2. 打开【目标群聊 -> 聊天成员 -> 查看更多】。
3. 运行 `python wechat_group_roster_audit.py` 做控件探测。
4. 需要保存群昵称时运行 `python wechat_group_roster_audit.py --export-nicknames`。

完成一次面板校准后，日常使用可以直接双击根目录的 `capture_panel.cmd`。它会读取
`panel_config.json` 中绑定的微信 PID，自动恢复该微信窗口，并把时间戳 PNG 保存到
`artifacts`。命令行等价操作是：

```powershell
python quick_capture.py
```

微信完全退出再启动后 PID 会变化。日常脚本在系统中仅有一个微信主窗口时会自动使用
新 PID；若检测到多个账号窗口，仍会拒绝猜测并要求传入 `-p PID`，避免操作错账号。
面板比例仍保留原校准值；窗口尺寸或布局变化时，应重新运行一次 `--calibrate-panel`。

按完整群名搜索并打开一个群聊：

```powershell
python open_group.py "Codex交流群2"
```

该命令只处理一个明确名称，使用本地 Tesseract OCR，并且只点击 `Group Chats` 或
`Most used` 会话分类中的名称匹配项，不会打开互联网搜索或聊天记录结果，也不会遍历
群名或读取成员资料。

快速统一入口使用短参数：

```powershell
# 默认：消息列表、群；OCR 精确打开一个群
python wx.py -q "Codex交流群2"

# 不 OCR，只输入搜索词并截图（最快）
python wx.py -q "Codex交流群2" -n

# 通讯录 Saved Groups，只截图该分区；离开分区即停止
python wx.py -m saved -n

# 双击 capture_saved_groups.cmd 也会执行上一条命令，并创建单独的时间戳输出目录

# 最多保存 3 张；这是保护上限，提前到底会自动停止
python wx.py -m saved -n -s 3

# 在最多 5 个保存群列表页面内 OCR 查找并选中一个群资料，保存右侧预览
# 群名在左侧被省略时，传一个可见且唯一的片段，例如 IT30
python wx.py -m saved -q "IT30" -s 5

# 好友模式：消息列表搜索好友
python wx.py -f -q "小张"

# 通讯录好友模式，不 OCR
python wx.py -m saved -f -q "小张" -n

# 使用 pywechat2 UIA 点击联系人并保存右侧资料截图（默认最多 10 个）
python uia_backup.py -s 10 -o artifacts/uia-contacts

# 点击保存的群聊条目并保存当前窗口截图
python uia_backup.py --groups -s 10 -o artifacts/uia-saved-groups
```

参数：`-m chat|saved` 选择来源（默认 `chat`），默认目标是群，`-f` 切换好友，
`-q` 指定单个搜索词，`-n` 跳过 OCR，`-s N` 是可选的截图上限，`-o` 指定输出目录。无
`-s` 时，截图会自动持续向下滚动，滚动条到底或列表不再变化时停止；`-m saved` 会先把
通讯录左栏回到顶部，并额外
限制在 `Saved Groups` 分区，进入普通联系人、公众号等下一分区时立即停止。内部仍有 1000 页
保护上限。输出中的 `stop_reason` 会说明是滚动条到底（`scrollbar_bottom`）、页面未变化
（`page_unchanged`）还是触发保护上限
（`maximum_pages`）。每次运行还会在输出目录写入 `result.json`，即使外部终端提前断开也可
查看最终页数与停止原因。
`-m saved -q` 会在最多 `-s` 个保存群列表页面内识别单个群名或可见唯一片段；未填写
`-s` 时该上限同样为 1000；
该页面没有独立搜索框，因此这种组合不能加 `-n`；每次查询会先回到保存群列表顶部、展开
`Saved Groups`/“保存的群聊”分区，且会对
分类内的群名文字区域局部放大后 OCR。跨页时会在离开“保存的群聊”分类后停止，不会继续匹配公众号或通讯录。`Saved Groups` 中的条目可能只是保存的群资料；此模式只选中条目并保存右侧预览，绝不会自动点 `Join Group`。无查询词时，`-m saved` 也只用 OCR 判断分区边界，输出只保留该分区的截图；它不会识别或导出条目资料。滚动等待为 0.25 秒并采用更大滚动距离。搜索或多页滚动后的截图使用当前桌面画面，避免新版微信的 `PrintWindow`
返回交互前旧帧；无查询的单页 `-n` 使用更快的窗口直出截图。无 OCR 搜索默认只等待
结果栏稳定所需的短时段，不会点击
搜索结果。`-m chat` 的 OCR 精确模式会输出 `clicked: true`，并通过 `title_verified`
核验聊天标题；若首次点击未切换，会只重试同一个已验证结果一次。`-m saved` 会输出 `selected: true`、`view: saved_group_preview` 和
`join_required`；它不会自动加入群。两种模式都保留 `opened_screenshot` 供人工确认。
当 `preview_state` 为 `join_group` 时，右侧绿色按钮表示该群资料尚未加入；脚本只记录
状态，绝不自动点击该按钮。

微信重启后通常不需要再传旧 PID。直接运行 `python wx.py -n`；只有同时打开多个微信
账号时才使用 `python wx.py -n -p 当前PID`。注意：`-o` 是输出目录，例如
`-o artifacts/run-1`；`-s 1` 表示最多只保存一张，不填 `-s` 才是自动到底。

`uia_backup.py` 是可选的 pywechat2 UIA 适配层，不改变 `wx.py` 的 OCR 默认流程。若
pywechat2 不在当前目录，可设置 `PYWECHAT2_ROOT` 或传 `--pywechat-root`：

```powershell
$env:PYWECHAT2_ROOT = "D:\tmp\anjian\pj\st\tmp\pywechat2"
& "$env:PYWECHAT2_ROOT\.venv\Scripts\python.exe" uia_backup.py -s 10
```

联系人备份会用 OCR 定位通讯录行，逐个打开联系人详情并保存整窗截图；不导出或解析 WXID、微信号、手机号等账号标识。
群成员详情目前暂未启用，群相关代码只保留列表截图和单群预览流程，GUI 不会误触发群成员操作。

源码调试不要把参数合并成一个字符串。可直接双击 `run_contact_debug.cmd`，它会以管理员权限运行并在结束后显示 `artifacts\debug\result.json`。`pythonw.exe` 没有控制台，闪退时不能用窗口是否出现判断成功与否。

### 跨机器使用与打包

运行 `build_portable.ps1` 会生成 `portable` 目录，里面有：

- `WechatRosterGUI.exe`：图形界面
- `wechat_backup_runner.exe`：唯一后端运行时，先尝试 UIA，UIA 不可见时自动回退 OCR
- `pywechat2\`：随包携带的源码和 Git 版本目录

整个 `portable` 目录可以复制到另一台 64 位 Windows 10/11 机器，不要求目标机安装
Python、pip 或外部 pywechat2 环境。目标机仍必须安装桌面微信、先手动登录，并允许桌面自动化；
程序不会替用户登录。GUI 的版本更新和切换直接操作包内的 `pywechat2\`，不需要重新打包 EXE。
截图功能不依赖目标机的 Python；有 Git 时优先使用 Git，无 Git 时 GUI 会通过 GitHub API
显示版本并下载源码。两种方式都使用代理配置。

便携版的截图按钮使用同目录的 `wechat_backup_runner.exe`，而不是系统 Python。GUI 中的版本
刷新/更新功能对当前配置的 pywechat2 checkout 执行 Git 操作；默认路径是包内的
`pywechat2\`。代理配置支持直连、HTTP、SOCKS5，默认
为 `127.0.0.1:7891`，只用于 Git 更新和依赖下载。

列出当前微信主窗口（包括缩到托盘后隐藏的窗口）：

```powershell
python wechat_group_roster_audit.py --list-windows
```

存在多个微信窗口时，从列表中取目标窗口的 `pid` 并传入 `--target-pid`。脚本不会
在多个账号之间猜测，以免截错窗口。
照片/视频查看器、聊天记录等使用相同 Qt 窗口类的辅助窗口会被排除。

激活明确选择的微信主窗口：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --activate-window
```

最小化的微信主窗口会标记 `"minimized": true`；缩到托盘的窗口会标记
`"hidden": true`。这两种状态下截图必须添加 `--activate-before-capture`，脚本会先显示或恢复明确
指定的窗口，再重新读取实际位置后截图；微信内部后台消息窗口仍会被排除。

把鼠标移动到右侧面板的左上角，然后读取相对坐标：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --calibrate-cursor
```

再把鼠标移动到右侧面板的右下角并运行一次。用两次输出的 `ratio` 计算：
`X=左上角x`、`Y=左上角y`、`WIDTH=右下角x-X`、`HEIGHT=右下角y-Y`。

也可以让脚本自动记录两次鼠标位置并生成 `panel_config.json`：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --calibrate-panel
```

命令启动后先在 5 秒内把鼠标放到面板左上角，再在下一段 5 秒内放到右下角。后续截图
会自动读取该配置；显式传入 `--panel` 时则以命令行参数为准。
配置记录校准窗口的 PID；切换到另一个微信账号窗口后不会复用旧配置，需要重新校准。
配置存在但损坏或 PID 不匹配时，脚本会明确报错，不会静默退回默认截图范围。

当前 UIA 树不可见时，可以按窗口比例对右侧面板做一次截图，用于校准和人工核对：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --capture-panel right-panel.png
```

截图前先激活目标微信，避免被其他窗口遮挡：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --capture-panel right-panel.png --activate-before-capture
```

截图源默认为 `--capture-source auto`：优先使用 Win32 `PrintWindow` 直接渲染目标窗口，
这样一般不会把终端、通知或远程控制浮层截进去；失败时自动退回桌面截图。也可以显式选择：

```powershell
# 只用 PrintWindow，失败就报错，不退回桌面截图
python wechat_group_roster_audit.py --target-pid 29588 --capture-panel right-panel.png --capture-source window

# 强制截取桌面上的窗口区域
python wechat_group_roster_audit.py --target-pid 29588 --capture-panel right-panel.png --capture-source screen
```

先生成带红框的整窗预览，可以直接检查截取范围是否正确：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --preview-panel panel-preview.png
```

默认截取窗口内 `(x=78%, y=14%, width=21%, height=82%)`。位置不准时可显式调整：

```powershell
python wechat_group_roster_audit.py --target-pid 29588 --capture-panel right-panel.png --panel 0.78 0.14 0.21 0.82
```

截图功能只截取窗口当前已渲染的内容，不滚动、不 OCR、不批量遍历搜索词，也不能恢复微信未显示或省略号截断的字段。
截图前会检查微信整窗和面板是否完全位于 Windows 虚拟桌面内；若窗口跨出屏幕边缘，
强制桌面截图会报告各方向缺失的像素数并拒绝生成残缺截图。`PrintWindow` 可以渲染
离屏部分；`auto` 模式仅在直接渲染失败且桌面截图会残缺时停止。

当前新版微信可能不暴露旧版 `WeChatMainWndForPC` 或 UIA 列表控件；此时程序会报告不兼容，不会尝试 Hook 或读取本地数据库。

## 新版微信兼容性结论

公开项目 [pywechat](https://github.com/Hello-Mr-Crab/pywechat) 已针对微信 4.x 改用 Win32
`FindWindow("Qt51514QWindowIcon", "Weixin")` 再连接 UIA，并使用 `mmui::*` 控件定位群成员页。
但其 `Weixin4.0.md` 同时说明：微信 4.1+ 可能按账号隐藏 UI Automation 树；如果当前账号的
UIA 树为空，任何公开的 `pywinauto`/`uiautomation` 选择器都会失败。该限制不能通过本项目
绕过，也不应通过 Hook、注入或读取本地数据库规避。

本项目先做只读探测，方便确认账号是否具备 UIA 能力；若探测不到控件，建议改用成员自愿
填写的授权表单或企业微信官方通讯录接口。

## 测试

```powershell
& "D:\Program Files\Python\Python311\python.exe" -m unittest -v
```
