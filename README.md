# 缅A决策看板 · 桌面版

把 [etf-option-board](https://github.com/NIO-SAI/etf-option-board) 包成一个双击就能用的本机 App：
不用装 Python、不用开命令行、不用记端口。Windows 和 macOS 都支持。

主项目仓库**完全不受影响**：这边不改它的代码逻辑，只通过三条契约跟它打交道。

---

## 为什么不直接在主项目里做

三层隔离，任何一层出问题都不会互相拖累：

| 层 | 做法 | 作用 |
| --- | --- | --- |
| 仓库 | 单独一个仓库（本仓库） | 桌面壳的依赖（pywebview / PyInstaller）不会进主项目，主项目的 CI 不会因为打包挂掉 |
| 代码 | 壳不 `import` 主项目任何模块 | 主项目内部重构，这边不用跟着改 |
| 运行时 | 打包时按 commit 取主项目源码到 `build/core/`，不进版本控制 | 三个月后不会出现「仓库里躺着一份过期的源码副本」 |

唯一的耦合点就三条，主项目只要不破坏它们，这边就永远能跑：

1. **启动命令**：`python -m uvicorn app:app --host 127.0.0.1 --port N`
2. **健康检查**：`GET /api/health` 返回 `{"ok": true}`
3. **数据目录**：认 `ETF_DATA_DIR` 环境变量（主项目 `paths.py`），且前后端同源、静态文件挂在 `/`

---

## 开发态跑起来

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # macOS: .venv/bin/pip

cp desktop.example.json desktop.json               # 改成你本机的路径
.venv/Scripts/python launcher/main.py              # macOS: .venv/bin/python
```

开发态不起子进程里的打包版，而是直接调用 `desktop.json` 里 `core_python` 指向的解释器起主项目的后端——
所以改完主项目代码，重启桌面壳就能看到效果，不用重新打包。

`desktop.json` 字段：

| 字段 | 说明 |
| --- | --- |
| `core_dir` | 主项目仓库路径（开发态用；打包后忽略） |
| `core_python` | 主项目虚拟环境的 python（开发态用） |
| `port` | 首选端口，被占用会自动往后找 20 个 |
| `title` / `width` / `height` | 窗口标题与尺寸 |
| `minimize_to_tray` | 关窗是缩到托盘（Windows）/ 隐藏窗口（macOS），还是直接退出 |

---

## 打 Windows 包

```bash
.venv/Scripts/python scripts/prepare_core.py            # 取主项目 master
.venv/Scripts/python -m PyInstaller packaging/etf_board.spec --noconfirm
```

产物 `dist/EtfBoard/`（约 51 MB），整个目录拷到别人机器上，双击 `EtfBoard.exe` 就能用。
**不需要**装 Python，也**不需要**装 WebView2（Win10 1803 以上自带）。

## 打 macOS 包

必须在 mac 上打（PyInstaller 不能交叉编译）：

```bash
bash scripts/build_mac.sh
```

产物 `dist/EtfBoard.app`，拖进「应用程序」即可。Dock 上显示的名字是中文。

**签名这件事，分三档，按需选：**

| 做法 | 效果 | 成本 |
| --- | --- | --- |
| 什么都不做 | 提示「已损坏，无法打开」，用户会以为中毒 | 0 |
| ad-hoc 签名（脚本已自动做） | 变成「无法验证开发者」，右键 →「打开」能过，只需一次 | 0 |
| Apple Developer ID 签名 + 公证 | 彻底无提示，双击即开 | $99/年 |

不想交 $99 的话，ad-hoc 那档够自己用；要发给不懂电脑的人，建议还是走公证。

**Intel / Apple Silicon**：两个架构要分别打。本仓库带了 GitHub Actions，
`.github/workflows/build.yml` 会在 `macos-13`（x64）和 `macos-14`（arm64）上各打一份，
手动触发（Actions → build → Run workflow）也行，不用自己找 Mac。

---

## 装到别人机器上

- Windows：把 `dist/EtfBoard/` 整个目录压缩发过去，解压后双击 `EtfBoard.exe`。
- macOS：把 `EtfBoard.app` 用 `ditto -c -k --keepParent` 压成 zip 再发（普通压缩会丢可执行位和符号链接）。

从网上下载的包，Windows 可能提示 SmartScreen、macOS 会拦一次，都是正常的「未签名」提示，
不是程序有问题——自己打给自己用的包跳过就行。

---

## 数据、日志在哪

程序装的地方是只读的，所以运行数据一律放用户目录：

| 平台 | 数据 | 日志 |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\EtfBoard\data` | `%LOCALAPPDATA%\EtfBoard\launcher.log` |
| macOS | `~/Library/Application Support/EtfBoard/data` | `~/Library/Application Support/EtfBoard/launcher.log` |

删掉数据目录 = 恢复出厂设置（推送配置、自选、历史记录都会清掉，注意备份 `push.json` 之类的文件）。

## 关窗之后程序还在跑

这是有意的：告警推送要靠后端采集行情，关掉就收不到了。

- **Windows**：关窗缩到右下角托盘，双击图标回来；真正退出要点托盘菜单的「退出」。
- **macOS**：关窗只是隐藏窗口，点菜单栏「看板 → 显示看板」回来；退出用「看板 → 退出」或 ⌘Q。

> macOS 为什么不用托盘图标：pystray 的 darwin 后端和 pywebview 都要求「事件循环跑在主线程」，
> 放一起会抢主线程互相卡死。所以 mac 上改用菜单栏常驻——功能一样，只是入口位置不同。

## 常见问题

**双击没反应 / 一闪就退**
看日志：Windows `%LOCALAPPDATA%\EtfBoard\launcher.log`，mac 同名目录。99% 是端口全被占（报错会写进日志）。

**提示「后端 60s 内未就绪」**
首次启动要建 SQLite、拉一次行情，网络差会慢。多试一次；还是不行就看日志里 uvicorn 的报错。

**打开了两个窗口**
不会。单实例锁（Windows 命名互斥体 / macOS 文件锁）保证只有一个进程，第二个会弹提示然后自己退出。

**想同时看两个看板 / 想换端口**
改 `desktop.json` 的 `port`。注意打包版读的是 exe 旁边的 `desktop.json`，改完重启生效。

**界面 404 或白屏**
打包时主项目缺少 `web/dist`。先在主项目里构建前端，再重跑 `prepare_core.py`。

---

## 目录结构

```
launcher/main.py        桌面壳：单实例 → 挑端口 → 起后端 → 开窗 → 托盘/菜单栏 → 收尾
scripts/prepare_core.py 按 commit 取主项目源码到 build/core（只拷运行需要的）
scripts/build_mac.sh    macOS 一键打包 + ad-hoc 签名
packaging/etf_board.spec PyInstaller 配置（Windows exe / macOS .app）
packaging/_warm.py      兜底入口：让分析器把三方依赖收全
.github/workflows/build.yml  Windows + macOS(x64/arm64) 三份产物
```
