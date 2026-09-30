"""Local, standard-library-only uninstaller. Never expose this through HTTP/MCP.

Run from a checkout with ``python3 -m boss_apply.uninstall --dry-run``.
The worker runs outside the installation so Windows can remove its .venv too.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field

PACKAGE = "oone-jobscout"
SOURCE = Path(__file__).resolve().parent.parent


class UninstallError(RuntimeError):
    pass


def run(args: list[str], timeout: int = 30) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if result.returncode:
        # Command output can contain user paths; never include config/secret contents.
        raise UninstallError(f"命令失败 ({result.returncode}): {args[0]} {' '.join(args[1:3])}")
    return result.stdout.strip()


def inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def safe_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.resolve() != path or is_link(path):
        raise UninstallError(f"拒绝删除通过符号链接或目录联接定位的根目录: {path}")
    home = Path.home().resolve()
    forbidden = {home, home / "Documents", home / "Desktop", home / "Downloads",
                 Path(sys.base_prefix).resolve()}
    system_paths = ["/", "/Users", "/home", "/tmp", "/var", "/usr", "/opt",
                    "/Applications", "/Library", "/System", "/Program Files",
                    "/Windows"]
    forbidden.update(Path(p).resolve() for p in system_paths)
    if os.name == "nt":
        forbidden.update(Path(os.environ[name]).resolve() for name in
                         ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "PUBLIC")
                         if os.environ.get(name))
    if path == Path(path.anchor):
        raise UninstallError(f"拒绝删除磁盘根目录: {path}")
    if path in forbidden or inside(home, path) or inside(Path(sys.base_prefix).resolve(), path):
        raise UninstallError(f"拒绝删除系统目录或用户主目录: {path}")
    if "site-packages" in path.parts or "dist-packages" in path.parts:
        raise UninstallError("拒绝递归删除共享 Python 包目录；uv 安装请用 --uv-only。")
    if not path.is_dir():
        raise UninstallError(f"目录不存在: {path}")
    return path


def checkout(path: Path) -> Path:
    path = safe_directory(path)
    project = path / "pyproject.toml"
    if (not project.is_file() or not re.search(r'^name\s*=\s*[\"\']oone-jobscout[\"\']',
            project.read_text(encoding="utf-8"), re.M) or
            not (path / "boss_apply" / "config.py").is_file() or
            not (path / "start.sh").is_file() or not (path / "start.bat").is_file()):
        raise UninstallError(f"不是可识别的 OONE-JobScout 源码安装目录: {path}")
    return path


def checkouts(root: Path) -> list[Path]:
    root = checkout(root)
    if not (root / ".git").exists():
        return [root]  # ZIP install
    git = shutil.which("git")
    if not git:
        raise UninstallError("检测到 Git 安装，但找不到 git；无法确认其他 Worktree。")
    # -z supports spaces, Unicode and newlines in worktree paths.
    output = run([git, "-C", str(root), "worktree", "list", "--porcelain", "-z"])
    paths = [checkout(Path(item[len("worktree "):])) for item in output.split("\0")
             if item.startswith("worktree ")]
    if root not in paths:
        raise UninstallError("Git Worktree 清单未包含当前目录，停止卸载。")
    return paths


@dataclass
class Target:
    path: str
    kind: str
    device: int
    inode: int


def target(path: Path, kind: str) -> Target:
    st = path.lstat()
    return Target(str(path), kind, st.st_dev, st.st_ino)


@dataclass
class Plan:
    targets: list[Target] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    uv: str = ""
    uv_tool: str = ""
    clean_cache: bool = False
    warnings: list[str] = field(default_factory=list)


def make_plan(root: Path | None, legacy: bool = False, uv_tools: bool = False) -> Plan:
    plan = Plan()
    if root is not None:
        source_roots = checkouts(root)
        for path in source_roots:
            plan.roots.append(str(path))
            plan.profiles.extend(str(path / "state" / name)
                                 for name in ("browser-profile", "console-profile"))
            plan.targets.append(target(path, "checkout"))
            for name in ("state", "config.local.json", "profile.local.json"):
                sensitive = path / name
                if is_link(sensitive) and not any(inside(sensitive.resolve(), p) for p in source_roots):
                    plan.warnings.append(f"外部数据链接目标保留: {sensitive} -> {sensitive.resolve()}")
            for name in ("browser-profile", "console-profile"):
                profile = path / "state" / name
                if is_link(profile) and not inside(profile.resolve(), path):
                    plan.warnings.append(f"外部浏览器数据链接目标保留: {profile.resolve()}")
    old_profile = Path.home() / "chrome-cdp-profile"
    if os.path.lexists(old_profile):
        if legacy:
            # A link is removed as a link, never followed to another browser profile.
            if not is_link(old_profile):
                safe_directory(old_profile)
            plan.targets.append(target(old_profile, "legacy-profile"))
            plan.profiles.append(str(old_profile))
        else:
            plan.warnings.append(f"旧浏览器数据仍在 {old_profile}；确认是本项目专用后加 --include-legacy-profile。")
    if uv_tools:
        uv = shutil.which("uv")
        if not uv:
            raise UninstallError("找不到 uv；无法核验全局安装或清理项目包缓存。")
        plan.uv = uv
        tools_dir = Path(run([uv, "--no-config", "tool", "dir"]))
        listing = run([uv, "--no-config", "tool", "list"])
        if any(line.startswith(PACKAGE + " ") for line in listing.splitlines()):
            tool = safe_directory(tools_dir / PACKAGE)
            if not (tool / "uv-receipt.toml").is_file():
                raise UninstallError("uv 工具目录缺少安装凭据，停止卸载。")
            plan.uv_tool = str(tool)
            plan.roots.append(str(tool))
            # Older builds kept state alongside site-packages.
            plan.profiles.extend(str(p.parent.parent / "state" / name)
                                 for p in tool.glob("**/boss_apply/config.py")
                                 for name in ("browser-profile", "console-profile"))
        plan.clean_cache = True
        plan.warnings.append("uv 按包清理缓存；共享依赖、Git 下载缓存及其他 uvx 环境不作无残留保证。")
    if not plan.roots and not plan.targets and not plan.clean_cache:
        raise UninstallError("没有可识别的安装目标。")
    return plan


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: str
    argv: tuple[str, ...]
    cwd: str = ""


def split_command(command: str) -> tuple[str, ...]:
    if os.name != "nt":
        # ps on POSIX loses quoting; recover only browser --user-data-dir in
        # ownership checks below, otherwise ambiguous paths fail closed.
        try:
            return tuple(shlex.split(command))
        except ValueError:
            return (command,)
    argc = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        return ()
    try:
        return tuple(argv[i] for i in range(argc.value))
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def native_argv(pid: int) -> tuple[str, ...]:
    """Read argument boundaries without ps's lossy whitespace representation."""
    if sys.platform.startswith("linux"):
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return tuple(os.fsdecode(v) for v in raw.rstrip(b"\0").split(b"\0"))
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), "sysctl KERN_PROCARGS2")
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), "sysctl KERN_PROCARGS2")
        raw = buf.raw[:size.value]
        argc = struct.unpack_from("i", raw)[0]
        offset = raw.index(b"\0", 4) + 1  # skip executable pathname
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        return tuple(os.fsdecode(v) for v in raw[offset:].split(b"\0")[:argc])
    raise UninstallError("仅支持 Windows、macOS 和 Linux。")


def powershell(script: str) -> str:
    exe = shutil.which("powershell.exe")
    if not exe:
        raise UninstallError("找不到 PowerShell，无法核验进程。")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])


def processes() -> list[Process]:
    if os.name == "nt":
        data = powershell("$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
            "CommandLine,@{n='Started';e={if ($_.CreationDate) {"
            "$_.CreationDate.ToUniversalTime().Ticks.ToString()} else {''}}}) "
            "| ConvertTo-Json -Compress")
        rows = json.loads(data)
        return [Process(int(p["ProcessId"]), int(p["ParentProcessId"]), str(p["Started"]),
                        split_command(p.get("CommandLine") or "")) for p in rows]
    # Native argv preserves Unicode, empty args, spaces and newlines. Limit
    # inspection to the current user; privileged/other-user installs are outside
    # this user-level uninstaller's scope.
    output = run(["ps", "-axo", "uid=,pid=,ppid=,stat=,lstart="])
    result = []
    for line in output.splitlines():
        fields = line.strip().split(None, 8)
        if len(fields) != 9 or int(fields[0]) != os.getuid():
            continue
        pid, parent = int(fields[1]), int(fields[2])
        if fields[3].startswith("Z"):
            continue
        try:
            args = native_argv(pid)
        except OSError:
            probe = subprocess.run(["ps", "-p", str(pid), "-o", "pid="],
                                   capture_output=True, timeout=5)
            if probe.returncode == 0 and probe.stdout.strip():
                raise UninstallError(f"无法读取当前用户的进程 PID {pid}，请关闭后重试。")
            continue
        cwd = ""
        if any("boss_apply" in arg or "scripts/" in arg or "scripts\\" in arg for arg in args):
            try:
                if sys.platform.startswith("linux"):
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                elif sys.platform == "darwin":
                    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
                    value = run([lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=5)
                    cwd = next((v[1:] for v in value.splitlines() if v.startswith("n")), "")
            except (OSError, UninstallError, subprocess.TimeoutExpired):
                pass
        result.append(Process(pid, parent, " ".join(fields[4:9]), args, cwd))
    return result


def owned(process: Process, plan: Plan) -> bool:
    args = process.argv
    if not args:
        return False
    # Exact dedicated profile, never browser executable name or port alone.
    for profile in plan.profiles:
        if f"--user-data-dir={profile}" in args:
            return True
        for i, arg in enumerate(args[:-1]):
            if arg == "--user-data-dir" and args[i + 1] == profile:
                return True
    roots = [Path(p) for p in plan.roots]
    exe = Path(args[0])
    scoped_exe = exe.is_absolute() and any(inside(exe, r) for r in roots)
    scoped_cwd = bool(process.cwd) and any(inside(Path(process.cwd), r) for r in roots)
    if "-m" in args:
        i = args.index("-m")
        if i + 1 < len(args) and (args[i + 1].startswith("boss_apply.") or
                                args[i + 1].startswith("scripts.")):
            return scoped_exe or scoped_cwd
    for arg in args[1:]:
        path = Path(arg)
        if path.suffix != ".py":
            continue
        if not path.is_absolute() and process.cwd:
            path = Path(os.path.abspath(Path(process.cwd) / path))
        if path.is_absolute() and any(inside(path, r / "scripts") or
                                     inside(path, r / "boss_apply") for r in roots):
            return True
    return scoped_exe and exe.name.lower().split(".")[0] in {
        "boss-apply", "boss-mcp", "oone-jobscout"}


def matches(plan: Plan, exclude: set[int]) -> list[Process]:
    table = processes()
    selected = {p.pid for p in table if p.pid not in exclude and owned(p, plan)}
    # Tunnel and browser helpers are descendants of an identified project process.
    changed = True
    while changed:
        changed = False
        for p in table:
            if p.parent in selected and p.pid not in selected and p.pid not in exclude:
                selected.add(p.pid)
                changed = True
    for p in table:
        if p.pid in exclude or p.pid in selected or p.cwd:
            continue
        if "-m" in p.argv:
            index = p.argv.index("-m")
            if index + 1 < len(p.argv) and p.argv[index + 1].startswith("boss_apply."):
                raise UninstallError(f"PID {p.pid} 正在运行 boss_apply，但无法确定安装归属。"
                                     "请先关闭该服务再卸载；不会按端口或模块名猜测并终止。")
    return [p for p in table if p.pid in selected]


def terminate(process: Process, force: bool = False) -> None:
    # Recheck start identity, guarding against stale heartbeat files / PID reuse.
    current = next((p for p in processes() if p.pid == process.pid), None)
    if current is None or current.started != process.started:
        return
    if os.name == "nt":
        # PowerShell checks identity again at termination; no /IM or taskkill /T.
        powershell(f"$ErrorActionPreference='Stop'; $p=Get-CimInstance Win32_Process "
            f"-Filter 'ProcessId={process.pid}'; if ($p -and "
            f"$p.CreationDate.ToUniversalTime().Ticks.ToString() -eq '{process.started}') "
            "{ $r=Invoke-CimMethod -InputObject $p -MethodName Terminate; "
            "if ($r.ReturnValue -ne 0) { throw 'Terminate failed' } }")
    else:
        try:
            os.kill(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop_processes(plan: Plan, exclude: set[int]) -> None:
    # Remember descendants even after their parent exits (e.g. mobile tunnel).
    found = {p.pid: p for p in matches(plan, exclude)}
    for p in found.values():
        terminate(p)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        table = {p.pid: p for p in processes()}
        alive = [p for p in found.values() if p.pid in table and
                 table[p.pid].started == p.started]
        newly_found = matches(plan, exclude)
        if not alive and not newly_found:
            return
        for p in newly_found:
            if p.pid not in found:
                found[p.pid] = p
                terminate(p)
        time.sleep(0.2)
    for p in found.values():
        terminate(p, force=True)
    time.sleep(0.3)
    table = {p.pid: p for p in processes()}
    if any(p.pid in table and table[p.pid].started == p.started for p in found.values()):
        raise UninstallError("项目进程仍在运行，未删除任何目录。请关闭后重试。")
    if matches(plan, exclude):
        raise UninstallError("检测到项目进程重新启动，未删除目录；请先停用外部进程管理器。")


def check_identity(item: Target) -> None:
    path = Path(item.path)
    st = path.lstat()
    if (st.st_dev, st.st_ino) != (item.device, item.inode):
        raise UninstallError(f"目录自预览后发生替换，拒绝删除: {path}")
    if item.kind == "checkout":
        checkout(path)
    elif item.kind == "legacy-profile":
        if path != Path.home() / "chrome-cdp-profile":
            raise UninstallError("无效的旧浏览器目录。")
        if not is_link(path):
            safe_directory(path)
    else:
        raise UninstallError("未知删除目标类型。")


def remove(path: Path) -> None:
    if is_link(path):
        if path.is_symlink():
            path.unlink()
        else:  # Windows junction: remove directory entry only.
            os.rmdir(path)
        return
    def readonly_retry(function, name, exc_info):
        if not isinstance(exc_info[1], PermissionError) or is_link(Path(name)):
            raise exc_info[1]
        os.chmod(name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(name)
    shutil.rmtree(path, onerror=readonly_retry)


def execute(plan: Plan, exclude: set[int]) -> int:
    errors = []
    # Validate every target before stopping services or deleting anything.
    for item in plan.targets:
        check_identity(item)
    stop_processes(plan, exclude)
    # Uninstall by package manager: it also removes console entrypoints.
    if plan.uv_tool:
        run([plan.uv, "--no-config", "tool", "uninstall", PACKAGE], timeout=120)
        if os.path.lexists(plan.uv_tool):
            raise UninstallError(f"uv 工具目录仍存在: {plan.uv_tool}")
    if plan.clean_cache:
        try:
            run([plan.uv, "--no-config", "cache", "clean", PACKAGE], timeout=120)
        except (UninstallError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    # External worktrees first, main checkout last. Nested roots are removed
    # individually first, ensuring shared .venv links cannot delete their target.
    for item in sorted(plan.targets, key=lambda t: len(Path(t.path).parts), reverse=True):
        try:
            check_identity(item)
            remove(Path(item.path))
        except (OSError, UninstallError) as exc:
            errors.append(f"未能删除 {item.path}: {exc}")
    for item in plan.targets:
        if os.path.lexists(item.path):
            errors.append(f"残留: {item.path}")
    if plan.uv_tool:
        listing = run([plan.uv, "--no-config", "tool", "list"])
        if any(line.startswith(PACKAGE + " ") for line in listing.splitlines()):
            errors.append("uv 全局工具仍在安装清单中。")
    if matches(plan, exclude):
        errors.append("检测到项目进程重新出现。")
    for error in errors:
        print(f"[失败] {error}", file=sys.stderr)
    if errors:
        print("卸载未完成，请按以上残留路径处理。", file=sys.stderr)
        return 1
    print("已删除所列安装目录与专属数据，已核验目录及进程。")
    if plan.warnings:
        print("仍有未覆盖项，不能宣称整台电脑无残留：")
        for warning in plan.warnings:
            print("  " + warning)
        return 2
    return 0


def worker(plan: Plan) -> int:
    # Use a base interpreter outside every deletion target, including uv tool
    # environments. No installs, downloads or administrator privileges needed.
    python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    roots = [Path(t.path) for t in plan.targets]
    if plan.uv_tool:
        roots.append(Path(plan.uv_tool))
    if not python.is_file() or any(inside(python, r) for r in roots):
        raise UninstallError("缺少安装目录外的 Python。请用系统 Python 3.10+ 运行卸载入口。")
    with tempfile.TemporaryDirectory(prefix="oone-uninstall-") as folder:
        temp = Path(folder)
        # Copy only this stdlib module; the worker never imports the application.
        script = temp / "uninstall.py"
        shutil.copyfile(Path(__file__), script)
        data = asdict(plan)
        data["parent_pid"] = os.getpid()
        plan_file = temp / "plan.json"
        plan_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(plan_file, 0o600)
        os.chdir(Path.home())
        return subprocess.call([str(python), "-B", str(script), "--worker", str(plan_file)],
                               cwd=Path.home())


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="OONE-JobScout 一键卸载（永久删除，请先预览）")
    parser.add_argument("--root", type=Path, help="源码安装目录；默认当前卸载入口所在项目")
    parser.add_argument("--dry-run", action="store_true", help="只查看范围，不停止进程、不删除文件")
    parser.add_argument("--yes", action="store_true", help="已确认预览范围，跳过交互确认")
    parser.add_argument("--include-legacy-profile", action="store_true", help="同时删除 ~/chrome-cdp-profile")
    parser.add_argument("--uv-tools", action="store_true", help="同时卸载 uv 全局工具、按包清理缓存")
    parser.add_argument("--uv-only", action="store_true", help="仅处理 uv 安装，不删除源码目录")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            data = json.loads(args.worker.read_text(encoding="utf-8"))
            parent = data.pop("parent_pid")
            data["targets"] = [Target(**t) for t in data["targets"]]
            return execute(Plan(**data), {os.getpid(), parent})
        if args.uv_only and args.root:
            raise UninstallError("--uv-only 与 --root 不能同时使用。")
        plan = make_plan(None if args.uv_only else (args.root or SOURCE),
                         args.include_legacy_profile, args.uv_tools or args.uv_only)
        running_python = Path(sys.executable).resolve()
        deletion_roots = [Path(t.path) for t in plan.targets]
        if plan.uv_tool:
            deletion_roots.append(Path(plan.uv_tool))
        if not args.dry_run and any(inside(running_python, r) for r in deletion_roots):
            base = Path(getattr(sys, "_base_executable", "")).resolve()
            if not base.is_file() or any(inside(base, r) for r in deletion_roots):
                raise UninstallError("请使用安装目录外的系统 Python 3.10+ 运行卸载入口。")
            # Replace this process; a waiting .venv Python would lock python.exe
            # on Windows and prevent the external worker from removing it.
            os.execv(str(base), [str(base), "-B", str(Path(__file__).resolve()),
                                *(sys.argv[1:] if argv is None else argv)])
        print("永久删除以下内容（包括目录内未提交代码、简历、密钥、登录态和 .venv）：")
        for item in plan.targets:
            print(f"  [{item.kind}] {item.path}")
        if plan.uv_tool:
            print(f"  [uv 工具及命令] {plan.uv_tool}")
        if plan.clean_cache:
            print(f"  [uv 包缓存] {PACKAGE}")
        for p in matches(plan, {os.getpid()}):
            print(f"  [停止专属进程] PID {p.pid}")
        for warning in plan.warnings:
            print("[未覆盖] " + warning)
        print("保留共享 Python/uv/浏览器/Tailscale；不清除系统日志、备份及第三方账户记录。")
        if args.dry_run:
            print("预览完成，未更改任何文件或进程。")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise UninstallError("非交互模式必须先 --dry-run，再显式使用 --yes。")
            if input("输入 DELETE 确认永久卸载，其他输入取消: ").strip() != "DELETE":
                print("已取消。")
                return 0
        return worker(plan)
    except (OSError, ValueError, UninstallError, subprocess.TimeoutExpired) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
