#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git Worktree 并行联调与分支管理工具。

用于团队多人并行开发、功能分支隔离调试、不同版本快速对比验证：
1. add    : 一键创建并行 Worktree 工作区，自动关联 .venv 并同步本地配置；
2. list   : 列出当前所有活跃的 Worktree 与分支状态；
3. remove : 安全注销并清理指定 Worktree 及其环境连接；
4. prune  : 清理失效的 Worktree 引用。
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _clear_readonly(path):
    """递归清除文件和目录的只读属性，避免 Windows 权限拒绝问题。"""
    if not path or not os.path.exists(path):
        return
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(f'attrib -r -s "{path}"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(f'attrib -r -s /s /d "{path}\\*"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass


def _safe_rmtree(path):
    """彻底安全删除目录树，解决 Windows 权限锁与残留问题。"""
    if not path or not os.path.exists(path):
        return
    _clear_readonly(path)
    try:
        shutil.rmtree(path)
    except Exception:
        _clear_readonly(path)
        if sys.platform == "win32":
            subprocess.run(f'cmd /c rd /s /q "{path}"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        try:
            os.rmdir(path)
        except Exception:
            pass


def _run_git(args, cwd=None, check=True):
    cmd = ["git"] + list(args)
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if check and res.returncode != 0:
        raise RuntimeError(f"Git 命令执行失败: {' '.join(cmd)}\n{res.stderr.strip()}")
    return res


def get_repo_root():
    res = _run_git(["rev-parse", "--show-toplevel"])
    return os.path.abspath(res.stdout.strip())


def get_git_common_dir():
    res = _run_git(["rev-parse", "--git-common-dir"])
    common = res.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.abspath(os.path.join(get_repo_root(), common))
    return common


def get_main_repo_root():
    common = get_git_common_dir()
    return os.path.dirname(common)


def link_venv(main_repo, target_dir):
    src_venv = os.path.join(main_repo, ".venv")
    target_venv = os.path.join(target_dir, ".venv")
    if not os.path.exists(src_venv):
        return False, "主仓库未找到 .venv，跳过环境映射"

    if os.path.exists(target_venv):
        return True, "工作区已存在 .venv"

    try:
        if sys.platform == "win32":
            # Windows NTFS 目录联接（Junction），无需管理员权限
            cmd = f'mklink /J "{target_venv}" "{src_venv}"'
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        else:
            os.symlink(src_venv, target_venv)
        return True, "已成功链接主仓库 .venv (零等待复用)"
    except Exception as e:
        return False, f"映射 .venv 失败: {e}"


def unlink_venv(target_dir):
    target_venv = os.path.join(target_dir, ".venv")
    if not (os.path.exists(target_venv) or os.path.islink(target_venv)):
        return
    try:
        if sys.platform == "win32":
            # 仅移除联接点，绝对不删除源文件夹中的文件
            subprocess.run(f'cmd /c rmdir "{target_venv}"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            if os.path.islink(target_venv):
                os.unlink(target_venv)
            elif os.path.isdir(target_venv):
                os.rmdir(target_venv)
    except Exception:
        pass


def cmd_add(args):
    main_repo = get_main_repo_root()
    branch = args.branch.strip()
    if not branch:
        print("❌ 错误：必须指定分支名称！")
        return 1

    # 确定工作区路径
    if args.path:
        target_dir = os.path.abspath(args.path)
    elif args.sibling:
        parent_dir = os.path.dirname(main_repo)
        safe_name = branch.replace("/", "-")
        target_dir = os.path.join(parent_dir, f"OONE-JobScout-{safe_name}")
    else:
        # 默认放在主仓库 .worktrees/ 目录下
        safe_name = branch.replace("/", "-")
        target_dir = os.path.join(main_repo, ".worktrees", safe_name)

    if os.path.exists(target_dir):
        print(f"❌ 错误：目标目录已存在: {target_dir}")
        return 1

    os.makedirs(os.path.dirname(target_dir), exist_ok=True)

    # 检查本地或远程分支是否存在
    b_check = _run_git(["branch", "--list", branch], cwd=main_repo, check=False)
    has_local = bool(b_check.stdout.strip())
    rem_check = _run_git(["branch", "-r", "--list", f"origin/{branch}"], cwd=main_repo, check=False)
    has_remote = bool(rem_check.stdout.strip())

    print(f"[*] 正在为分支 [{branch}] 创建 Git Worktree 工作区...")

    git_add_cmd = ["worktree", "add"]
    if has_local:
        git_add_cmd += [target_dir, branch]
    elif has_remote:
        git_add_cmd += ["--track", "-b", branch, target_dir, f"origin/{branch}"]
    else:
        base = args.base or "HEAD"
        git_add_cmd += ["-b", branch, target_dir, base]

    _run_git(git_add_cmd, cwd=main_repo)

    # 自动关联主仓库 .venv
    v_ok, v_msg = link_venv(main_repo, target_dir)

    # 自动同步本地配置文件 (config.local.json)
    cfg_src = os.path.join(main_repo, "config.local.json")
    cfg_dst = os.path.join(target_dir, "config.local.json")
    cfg_synced = False
    if os.path.isfile(cfg_src) and not os.path.exists(cfg_dst):
        try:
            shutil.copy2(cfg_src, cfg_dst)
            cfg_synced = True
        except Exception:
            pass

    print("\n" + "=" * 68)
    print("  🎉 Git Worktree 并行工作区已创建成功！")
    print("=" * 68)
    print(f"  📌 分支名称: {branch}")
    print(f"  📂 物理路径: {target_dir}")
    print(f"  🐍 运行环境: {v_msg}")
    if cfg_synced:
        print("  🔑 本地配置: 已自动同步主仓库 config.local.json（API Key 就绪）")
    print("=" * 68)
    print("\n🚀 快速进入并开展工作：")
    print(f"   cd \"{target_dir}\"")
    if sys.platform == "win32":
        print("   start.bat\n")
    else:
        print("   ./start.sh\n")
    return 0


def cmd_list(args):
    main_repo = get_main_repo_root()
    res = _run_git(["worktree", "list", "--porcelain"], cwd=main_repo)
    raw = res.stdout.strip().split("\n\n")

    worktrees = []
    for block in raw:
        if not block.strip():
            continue
        wt = {}
        for line in block.strip().split("\n"):
            parts = line.split(" ", 1)
            key = parts[0]
            val = parts[1] if len(parts) > 1 else True
            wt[key] = val
        worktrees.append(wt)

    print("\n" + "=" * 76)
    print("  📋 当前项目 Git Worktree 并行工作区列表")
    print("=" * 76)
    print(f" {'类型':<6} | {'分支':<24} | {'Commit':<10} | {'工作区路径'}")
    print("-" * 76)

    for i, wt in enumerate(worktrees):
        path = wt.get("worktree", "")
        branch = wt.get("branch", "").replace("refs/heads/", "")
        head = wt.get("HEAD", "")[:7]
        is_bare = "bare" in wt
        is_main = (os.path.normpath(path) == os.path.normpath(main_repo))
        tag = "主仓库" if is_main else f"WT #{i}"
        print(f" {tag:<6} | {branch:<24} | {head:<10} | {path}")

    print("=" * 76 + "\n")
    return 0


def cmd_remove(args):
    main_repo = get_main_repo_root()
    name = args.name.strip()
    if not name:
        print("❌ 错误：必须指定要移除的分支名或工作区路径！")
        return 1

    res = _run_git(["worktree", "list", "--porcelain"], cwd=main_repo)
    raw = res.stdout.strip().split("\n\n")
    target_path = None
    target_branch = None

    for block in raw:
        if not block.strip():
            continue
        wt = {}
        for line in block.strip().split("\n"):
            parts = line.split(" ", 1)
            wt[parts[0]] = parts[1] if len(parts) > 1 else True
        p = wt.get("worktree", "")
        b = wt.get("branch", "").replace("refs/heads/", "")
        if name in (p, b, os.path.basename(p)):
            target_path = p
            target_branch = b
            break

    if not target_path:
        # 兼容未被 worktree list 记录但文件夹存在的孤儿目录
        potential = os.path.join(main_repo, ".worktrees", name)
        if os.path.isdir(potential):
            target_path = potential

    if not target_path:
        print(f"❌ 错误：未找到匹配的 Worktree: {name}")
        return 1

    if os.path.normpath(target_path) == os.path.normpath(main_repo):
        print("❌ 错误：严禁删除主仓库工作区！")
        return 1

    print(f"[*] 正在注销 Worktree: {target_path} (分支: {target_branch or '未知'})...")

    # 1. 安全解除 .venv 软联接
    unlink_venv(target_path)

    # 2. 清除 Windows 只读属性
    _clear_readonly(target_path)
    git_worktree_meta = os.path.join(main_repo, ".git", "worktrees")
    _clear_readonly(git_worktree_meta)

    # 3. 移除 worktree
    force_flag = ["--force"] if args.force else []
    try:
        _run_git(["worktree", "remove"] + force_flag + [target_path], cwd=main_repo)
    except Exception as e:
        err_msg = str(e)
        if "contains modified or untracked files" in err_msg and not args.force:
            print("❌ 错误：工作区包含未提交或未跟踪的本地文件（如自动同步的 config.local.json）。")
            print("👉 若确认丢弃并彻底删除，请添加 --force (-f) 参数，例如：")
            print(f"   wt remove {name} --force -d")
            return 1
        # 若因 Windows 文件锁或只读属性导致 git 内部删除异常，执行安全兜底
        print(f"⚠️  Git 移除非致命提示，正在执行安全兜底清理...")
        _safe_rmtree(target_path)
        _clear_readonly(git_worktree_meta)
        _run_git(["worktree", "prune"], cwd=main_repo, check=False)

    # 4. 若物理文件夹仍有残留（例如被 .gitignore 忽略的本地运行日志/配置），彻底清理
    _safe_rmtree(target_path)

    # 5. 修剪失效的 Git Worktree 元数据引用
    _clear_readonly(git_worktree_meta)
    _run_git(["worktree", "prune"], cwd=main_repo, check=False)

    # 6. 若指定同时删除分支
    if args.delete_branch and target_branch:
        del_flag = "-D" if args.force else "-d"
        b_res = _run_git(["branch", del_flag, target_branch], cwd=main_repo, check=False)
        if b_res.returncode == 0:
            print(f"[*] 已同步清理本地分支: {target_branch}")
        else:
            print(f"⚠️  清理分支提示: {b_res.stderr.strip()}")

    print(f"✅ Worktree 已安全移除: {target_path}")
    return 0


def cmd_prune(args):
    main_repo = get_main_repo_root()
    git_worktree_meta = os.path.join(main_repo, ".git", "worktrees")
    _clear_readonly(git_worktree_meta)
    res = _run_git(["worktree", "prune", "-v"], cwd=main_repo, check=False)
    out = res.stdout.strip()
    print(out if out else "✅ 所有 Worktree 引用均有效，无需清理。")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="OONE-JobScout Git Worktree 并行联调工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/worktree.py add feat/ai-reply       # 创建并初始化并行分支工作区
  python scripts/worktree.py list                    # 查看当前所有工作区
  python scripts/worktree.py remove feat/ai-reply    # 移除指定工作区
  python scripts/worktree.py prune                   # 清理失效的工作区引用
"""
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    # add
    p_add = subparsers.add_parser("add", help="创建新 Worktree 并自动配置环境")
    p_add.add_argument("branch", help="分支名称 (如 feat/scorer-optimize)")
    p_add.add_argument("--base", "-b", help="基于哪个分支创建 (默认当前 HEAD)")
    p_add.add_argument("--path", "-p", help="显式指定目标文件夹路径")
    p_add.add_argument("--sibling", "-s", action="store_true", help="在同级目录创建 (如 ../OONE-JobScout-<branch>)")

    # list
    subparsers.add_parser("list", help="列出当前所有工作区")

    # remove
    p_rm = subparsers.add_parser("remove", help="移除指定 Worktree")
    p_rm.add_argument("name", help="分支名或路径")
    p_rm.add_argument("--force", "-f", action="store_true", help="强制删除 (含未提交修改)")
    p_rm.add_argument("--delete-branch", "-d", action="store_true", help="同时删除对应 Git 分支")

    # prune
    subparsers.add_parser("prune", help="清理失效的 Worktree 引用")

    args = parser.parse_args()
    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "add":
        return cmd_add(args)
    elif args.subcommand == "list":
        return cmd_list(args)
    elif args.subcommand == "remove":
        return cmd_remove(args)
    elif args.subcommand == "prune":
        return cmd_prune(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
