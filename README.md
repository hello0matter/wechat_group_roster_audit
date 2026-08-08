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

微信完全退出再启动后 PID 会变化。此时一键脚本会明确报错；先用 `--list-windows`
取得当前 PID，再对目标窗口重新运行一次 `--calibrate-panel`，不会自动猜测其他账号。

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

# 通讯录 Saved Groups，保存 3 张向下滚动后的可见页截图
python wx.py -m saved -n -s 3

# 在最多 5 个保存群列表页面内 OCR 查找并选中一个群资料，保存右侧预览
# 群名在左侧被省略时，传一个可见且唯一的片段，例如 IT30
python wx.py -m saved -q "IT30" -s 5

# 好友模式：消息列表搜索好友
python wx.py -f -q "小张"

# 通讯录好友模式，不 OCR
python wx.py -m saved -f -q "小张" -n
```

参数：`-m chat|saved` 选择来源（默认 `chat`），默认目标是群，`-f` 切换好友，
`-q` 指定单个搜索词，`-n` 跳过 OCR，`-s N` 保存 N 张滚动后的可见页截图，`-o`
指定输出目录。`-m saved -q` 会在最多 `-s` 个保存群列表页面内识别单个群名或可见唯一片段；
该页面没有独立搜索框，因此这种组合不能加 `-n`；每次查询会先回到保存群列表顶部，且会对
分类内的群名文字区域局部放大后 OCR。跨页时会在离开“保存的群聊”分类后停止，不会继续匹配公众号或通讯录。`Saved Groups` 中的条目可能只是保存的群资料；此模式只选中条目并保存右侧预览，绝不会自动点 `Join Group`。无查询词时，多页截图只保存左侧列表区域，并按接近一页的
距离滚动。搜索或多页滚动后的截图使用当前桌面画面，避免新版微信的 `PrintWindow`
返回交互前旧帧；无查询的单页 `-n` 使用更快的窗口直出截图。无 OCR 搜索默认只等待
结果栏稳定所需的短时段，不会点击
搜索结果。`-m chat` 的 OCR 精确模式会输出 `clicked: true`，并通过 `title_verified`
核验聊天标题；若首次点击未切换，会只重试同一个已验证结果一次。`-m saved` 会输出 `selected: true`、`view: saved_group_preview` 和
`join_required`；它不会自动加入群。两种模式都保留 `opened_screenshot` 供人工确认。
当 `preview_state` 为 `join_group` 时，右侧绿色按钮表示该群资料尚未加入；脚本只记录
状态，绝不自动点击该按钮。

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
