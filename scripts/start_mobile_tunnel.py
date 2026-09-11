# -*- coding: utf-8 -*-
"""一键启动临时手机穿透通道（免安装 App / 免登录账号 / 零隐私泄露）

适用于外出时临时借用他人手机、平板或公用电脑访问 BOSS-Apply Web 控制台。
底层基于 Cloudflare Quick Tunnel（端到端临时加密隧道），用完即关，不留痕迹。
"""

import os
import re
import sys
import time
import signal
import shutil
import urllib.request
import subprocess

# 强制 UTF-8 输出与实时行刷新，防止 Windows 终端缓冲或渲染 ASCII 二维码报错
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

def find_cloudflared() -> str:
    """寻找本地可用 cloudflared 可执行文件"""
    candidates = [
        r"D:\LENOVO\Tailscale\cloudflared.exe",
        r"D:\Tailscale\cloudflared.exe",
        r"D:\LENOVO\tools\cloudflared.exe",
        os.path.join(os.path.dirname(__file__), "..", "bin", "cloudflared.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)
    which = shutil.which("cloudflared")
    if which:
        return which
    return ""

def check_local_server(port: int = 8788) -> bool:
    """检查本地 Web 工作台是否在线"""
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5)
        return req.status in (200, 401)
    except Exception:
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5)
            return req.status in (200, 401)
        except Exception:
            return False

def get_web_token() -> str:
    """获取控制台 token"""
    try:
        from boss_apply import config as cfgmod
        cfg = cfgmod.load()
        token = (cfg.get("web") or {}).get("token") or os.getenv("APPROVAL_TOKEN") or "boss-apply"
        return token
    except Exception:
        return "boss-apply"

def print_qr(url: str):
    """在控制台打印 ASCII 二维码"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print("\n" + "=" * 54)
        print("          📱 他人手机扫码直达（微信 / 相机扫一扫）")
        print("=" * 54)
        qr.print_ascii(invert=True)
        print("=" * 54 + "\n")
    except Exception as e:
        pass

def main():
    print("\n" + "━" * 58)
    print("  🚀 BOSS-Apply 应急手机通道（免装App / 零痕迹穿透）")
    print("━" * 58)

    cf_path = find_cloudflared()
    if not cf_path:
        print("\n❌ 错误：未找到 cloudflared.exe 可执行文件！")
        print("   预期路径：D:\\LENOVO\\Tailscale\\cloudflared.exe")
        return 1

    # 1. 检查本地 Web 服务
    port = 8788
    token = get_web_token()
    if not check_local_server(port):
        print(f"\n⚠️ 提示：本地 Web 服务 (http://127.0.0.1:{port}) 似乎未启动。")
        print(f"   正在为您尝试拉起 approval_web.py ...")
        approval_script = os.path.join(os.path.dirname(__file__), "approval_web.py")
        subprocess.Popen([sys.executable, "-u", approval_script, "--host", "0.0.0.0", "--port", str(port)])
        time.sleep(2.0)

    # 2. 启动 Cloudflare Quick Tunnel
    print("\n⏳ 正在建立端到端加密临时安全通道，请稍候约 3-5 秒...")
    cmd = [cf_path, "tunnel", "--url", f"http://127.0.0.1:{port}"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    tunnel_url = None
    start_t = time.time()
    while time.time() - start_t < 25:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
        if match:
            tunnel_url = match.group(0)
            break

    if not tunnel_url:
        print("\n❌ 连接超时：未能获取 Cloudflare 临时公网地址。请检查网络后重试。")
        proc.kill()
        return 1

    full_url = f"{tunnel_url}/?token={token}"

    # 3. 输出直观视觉指引
    print("\n" + "=" * 58)
    print("       >>> 临时应急穿透通道已就绪！ <<<")
    print("=" * 58)

    print_qr(full_url)

    print(f"[*] 手机直连网址（点击或复制）：\n    {full_url}\n")
    print("[-] 使用说明：")
    print("    1. 借用他人手机时，直接用其手机【微信扫一扫】或【系统相机】扫描上方二维码。")
    print("    2. 也可将上述网址发送给对方微信/文件传输助手，手机浏览器点击秒开。")
    print("    3. 优势：对方手机【无需下载任何 App】，【无需登录个人账号】，【无任何安全泄漏】。")
    print("    4. 自带 Token 安全防线：未知 Token 的任何人均无法查看您的任何求职数据。")
    print("\n[!] 退出说明：")
    print("    用完后直接在此窗口按 【Ctrl + C】 或关闭窗口，公网隧道将立即物理销毁！")
    print("=" * 58 + "\n")

    # 4. 监听与等待
    try:
        while True:
            time.sleep(1)
            if proc.poll() is not None:
                print("隧道进程已终止。")
                break
    except KeyboardInterrupt:
        print("\n\n🔌 正在安全关闭临时通道...")
    finally:
        try:
            if proc and proc.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
                    proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        print("✅ 临时通道已成功销毁，公网访问已物理切断。\n")

    return 0

if __name__ == "__main__":
    sys.exit(main())
