# OONE-JobScout 卸载说明

卸载入口删除预览清单中的整个安装目录，不移入废纸篓，不保存卸载日志或配置备份。目录中的个人文件、未提交代码和 Git 历史会一并删除。需要保留的内容必须提前移到清单以外。

## 删除范围

| 内容 | 处理方式 |
| --- | --- |
| 当前源码安装目录 | 校验项目标识后，删除完整目录，包括 `.git`、`.venv`、私有配置、密钥、简历、日志和台账 |
| Git Worktree | 使用 Git 实际登记清单，列出并删除同一仓库的全部工作区，包含外部工作区；从 Worktree 启动也会显示主仓库 |
| 新版调试浏览器 | 调试浏览器数据在 `state/browser-profile`，工作台 App 窗口数据在 `state/console-profile`，均位于安装目录；关闭专属进程后一起删除 |
| 旧版 `~/chrome-cdp-profile` | 仅在显式使用 `--include-legacy-profile` 后删除；该目录没有可靠的历史所有权凭据，使用者必须确认没有共用 |
| uv 全局安装 | 使用 `--uv-tools` 或 `--uv-only`：定位 uv 管理目录与安装凭据，再执行 `uv tool uninstall oone-jobscout` 移除工具环境和命令入口 |
| uv 项目包缓存 | 同上，执行 `uv cache clean oone-jobscout`，不执行全缓存清空或强制绕过缓存锁 |
| 项目内的链接/Windows 目录联接 | 删除链接本身，保留外部目标；检测到外部敏感数据链接会显示未覆盖项 |
| 项目进程及子进程 | 校验参数、安装路径/工作目录与进程启动身份；停止服务、守护进程、专属浏览器及其仍可识别的子进程 |

安装目录之外的临时 worker 在卸载期间承担删除任务，使用已有的基础 Python，不创建新环境、不联网安装依赖。完成后清除 worker 和计划文件。Windows 下先离开安装目录，批处理入口定位并使用安装目录外的基础 Python，避免虚拟环境重定向进程锁住 `.venv`；直接从将被删除的虚拟环境执行卸载会明确拒绝。

## 使用方式

1. Windows 双击 `uninstall.bat`，macOS 双击 `uninstall.command`，Linux 运行 `bash uninstall.sh`。
2. 审阅终端列出的完整路径及未覆盖项。默认入口删除源码安装；旧浏览器数据和 uv 全局安装需要使用对应选项。
3. 输入 `DELETE`。其他输入取消。非交互模式要求显式 `--yes`。

示例（在项目根目录）：

```bash
python3 -B boss_apply/uninstall.py --dry-run
python3 -B boss_apply/uninstall.py --include-legacy-profile --uv-tools --dry-run
python3 -B boss_apply/uninstall.py --include-legacy-profile --uv-tools
```

Windows 将 `python3 -B` 改为 `py -3 -B`。`--root /完整/安装路径` 可定位其他源码安装；此参数拒绝系统目录、用户主目录、共享 site-packages 及符号链接根目录。多个独立 Git 克隆、不同账户或自定义 uv 管理/缓存目录需要分别运行，不能靠全盘匹配名称安全识别。

uv 新版安装提供 `oone-uninstall --uv-only`。旧版安装可使用本源码中的卸载脚本并加 `--uv-only`。Windows 的 `oone-uninstall.exe` 属于被删除的工具环境，请使用安装目录外的系统 Python 运行本源码卸载脚本并加 `--uv-only`。不要用 uvx 启动卸载器。

升级时请先关闭旧版调试浏览器再启动新版；如果旧实例仍在监听 9335，原有连通性检测仍可能复用它，数据仍在旧目录。

## 返回码与失败处理

| 返回码 | 含义 |
| --- | --- |
| `0` | 预览完成、用户取消，或所列安装/数据目标删除并核验通过；这是清单范围的结果 |
| `1` | 失败：权限、文件锁、进程归属未知、目录替换、残留或缓存命令失败；输出会列出失败原因/残留路径 |
| `2` | 所列目标已清理，但发现未覆盖项，例如保留旧浏览器数据、外部数据链接，或 uv 按包清缓存无法验证全部缓存痕迹 |

无法枚举进程、识别 Worktree、确认目录身份或停止进程时，不开始目录删除。出现部分删除失败时继续核验并返回失败，不报“全部成功”。先处理终端指出的权限、文件锁或外部进程管理器，再从保留的源码副本运行卸载脚本。若主目录已经移除，请重新取得卸载脚本，指定实际仍存在的目标路径。

Windows 不能可靠读取任意系统 Python 进程的当前工作目录；如果发现无法确定安装归属的 `python -m boss_apply...`，会要求先关闭服务，不猜测并终止。已退出并重新归属其他进程的 PID 不会因为旧记录而被杀死。已脱离父进程、没有项目路径标识的历史隧道或自定义进程不保证自动识别。

## 无残留的边界

可核验的是预览清单内的项目文件和可识别进程。文件删除不是安全擦除，不能保证 SSD、系统快照或备份中的数据无法恢复。共享 Python、uv、普通浏览器、Tailscale、外部 cloudflared 和依赖缓存会保留。项目目录内下载的 `bin/`、`tools/` 会随目录删除。

uv 按包清缓存以 uv 的包关联规则为准，不能证明 Git 下载缓存、其他 uvx 临时环境及共享依赖的所有副本均已消失，因此 uv 模式显示未覆盖项并返回 `2`。不使用 `uv cache clean` 无参数清空共享缓存。参考 [uv 官方工具与缓存命令](https://docs.astral.sh/uv/reference/cli/)。

手动添加到编辑器的 MCP 配置、独立复制的项目、下载压缩包、外部简历原件、终端/普通浏览器历史、系统日志、Time Machine/还原点，以及 BOSS/飞书/模型服务端记录，无法由本地卸载器安全、完整地清除。远程 CDP 浏览器也不属于本机专属数据。卸载不撤销已发生的投递、不删除远程账号或吊销服务端 API 密钥。

卸载只提供本地命令和文件入口，不通过 Web API 或 MCP 暴露删除能力。

## 验证

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/integration_uninstall.py
```

单元测试使用临时目录，覆盖范围预览、取消、误删保护、共享环境链接、旧 Profile、外部敏感数据链接、PID 复用、子进程、Worktree、锁定文件和 uv 按包命令。集成测试只启动并删除自行创建的临时安装和测试进程，覆盖虚拟环境自卸载、Unicode/空格路径、专属进程停止及无关进程保留，不启动真实浏览器或访问 BOSS。

GitHub Actions 的 macOS/Linux/Windows 测试矩阵已全部通过，包含 16 项单元测试（Windows 跳过 2 项 POSIX 符号链接用例）及隔离集成测试。Windows 集成测试使用实际 uninstall.bat 入口，并验证从待删除的虚拟环境直接卸载会被拒绝。验证记录：https://github.com/MCxxxDT/OONE-JobScout/actions/runs/36715332772 。
