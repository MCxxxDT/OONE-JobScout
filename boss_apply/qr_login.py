"""BOSS直聘 扫码鉴权管理模块。

职责：
1. QRLoginManager.get_qrcode(cfg) -> dict:
   - 在 Chrome CDP (默认 9335 端口) 创建隔离上下文并导航至登录页；
   - 自动切换至二维码扫码态，捕获原生二维码图片的 Base64 数据与唯一 UUID；
   - 返回 {"ok": True, "uuid": uuid, "qrcode_base64": b64_img, "expire_seconds": 180}；
2. QRLoginManager.check_scan_status(uuid, cfg) -> dict:
   - 轮询扫码状态：waiting(等待扫码) / scanned(已扫码待手机确认) / confirmed(登录成功) / expired(已过期)；
   - 登录成功后自动抓取完整 Session Cookie (wt2, geek_zp_token, zp_token, zp_at 等)；
   - 使用 Windows DPAPI (boss_apply/secrets.py) 加密持久化落盘至 state/secrets.json；
   - 自动原子化注入主 Chrome 实例，零重启热生效；
3. QRLoginManager.inject_cookies_to_cdp(cookies, cfg) -> bool:
   - 通过 CDP Network.setCookies / Storage.setCookies 原子化注入 Chrome，零重启热生效；
4. QRLoginManager.get_auth_status(cfg) -> dict:
   - 轻量探测当前 Chrome CDP 或 DPAPI 持久化 Cookie 是否处于有效登录态。
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
from urllib.request import urlopen

from . import config as cfgmod, profile_store, rawcdp, secrets as secrets_mod

LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
COOKIE_SECRET_KEY = "boss_session_cookies"
USER_PROFILE_CACHE = os.path.join(cfgmod.STATE_DIR, "user_profile.json")


def ensure_chrome_running(cfg=None) -> bool:
    """自动检测 9335 端口连通性；若未运行，自动以后台静默方式拉起 Chrome 实例并等待就绪。"""
    cfg = cfg or cfgmod.load()
    cdp_http = cfg.get("cdp_endpoint", "http://127.0.0.1:9335")

    try:
        urlopen(cdp_http + "/json/version", timeout=1.5)
        return True
    except Exception:
        pass

    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        os.path.expanduser("~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        "/Applications/Arc.app/Contents/MacOS/Arc",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/microsoft-edge",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    chrome_exe = None
    for cand in chrome_candidates:
        if os.path.exists(cand):
            chrome_exe = cand
            break

    if not chrome_exe:
        return False

    profile_dir = os.path.expanduser("~/chrome-cdp-profile")
    if not os.path.exists(profile_dir):
        try:
            os.makedirs(profile_dir, exist_ok=True)
        except Exception:
            pass

    is_silent = bool((cfg.get("browser") or {}).get("silent_mode", True))
    is_min = bool((cfg.get("browser") or {}).get("minimize_on_start", True))
    window_arg = "--start-minimized" if (is_silent or is_min) else "--start-maximized"
    cmd = [
        chrome_exe,
        "--remote-debugging-port=9335",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--remote-allow-origins=*",
        window_arg,
        "https://www.zhipin.com/web/geek/jobs"
    ]

    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL
    }
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes
            kernel32 = ctypes.windll.kernel32

            class STARTUPINFOW(ctypes.Structure):
                _fields_ = [
                    ('cb', ctypes.wintypes.DWORD), ('lpReserved', ctypes.c_wchar_p),
                    ('lpDesktop', ctypes.c_wchar_p), ('lpTitle', ctypes.c_wchar_p),
                    ('dwX', ctypes.wintypes.DWORD), ('dwY', ctypes.wintypes.DWORD),
                    ('dwXSize', ctypes.wintypes.DWORD), ('dwYSize', ctypes.wintypes.DWORD),
                    ('dwXCountChars', ctypes.wintypes.DWORD), ('dwYCountChars', ctypes.wintypes.DWORD),
                    ('dwFillAttribute', ctypes.wintypes.DWORD), ('dwFlags', ctypes.wintypes.DWORD),
                    ('wShowWindow', ctypes.wintypes.WORD), ('cbReserved2', ctypes.wintypes.WORD),
                    ('lpReserved2', ctypes.c_void_p), ('hStdInput', ctypes.wintypes.HANDLE),
                    ('hStdOutput', ctypes.wintypes.HANDLE), ('hStdError', ctypes.wintypes.HANDLE),
                ]

            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ('hProcess', ctypes.wintypes.HANDLE), ('hThread', ctypes.wintypes.HANDLE),
                    ('dwProcessId', ctypes.wintypes.DWORD), ('dwThreadId', ctypes.wintypes.DWORD),
                ]

            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(STARTUPINFOW)
            si.lpDesktop = 'WinSta0\\Default'
            si.dwFlags = 1  # STARTF_USESHOWWINDOW
            # 0 = SW_HIDE (静默模式：真正的前台隐藏运行，零弹窗跳屏)
            # 6 = SW_MINIMIZE (前台模式下的启动时最小化)
            # 3 = SW_SHOWMAXIMIZED (常规最大化启动)
            if is_silent:
                si.wShowWindow = 0
            elif is_min:
                si.wShowWindow = 6
            else:
                si.wShowWindow = 3

            pi = PROCESS_INFORMATION()
            cmd_str = subprocess.list2cmdline(cmd)
            # 0x00000208 = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            res = kernel32.CreateProcessW(None, cmd_str, None, None, False, 0x00000208, None, None, ctypes.byref(si), ctypes.byref(pi))
            if res:
                kernel32.CloseHandle(pi.hProcess)
                kernel32.CloseHandle(pi.hThread)
            else:
                popen_kwargs["creationflags"] = 0x00000208
                subprocess.Popen(cmd, **popen_kwargs)
        except Exception:
            popen_kwargs["creationflags"] = 0x00000208
            subprocess.Popen(cmd, **popen_kwargs)
    else:
        subprocess.Popen(cmd, **popen_kwargs)

    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            urlopen(cdp_http + "/json/version", timeout=1)
            if is_silent:
                try:
                    from boss_apply import rawcdp
                    rawcdp.set_win32_browser_visibility(port=9335, visible=False)
                except Exception:
                    pass
            return True
        except Exception:
            continue
    return False


def get_cached_user_profile() -> dict:
    """读取已缓存的 BOSS 用户个人画像，若无缓存返回系统画像默认值。"""
    if os.path.exists(USER_PROFILE_CACHE):
        try:
            with open(USER_PROFILE_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("name"):
                    return data
        except Exception:
            pass

    # 若无缓存，优先从 profile.local.json 的 refined 中获取
    p_path = profile_store.PROFILE_PATH
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)
                refined = p_data.get("refined", {})
                r_name = refined.get("name")
                if r_name:
                    return {
                        "name": r_name,
                        "avatar": "",
                        "school": refined.get("school", ""),
                        "major": refined.get("major", ""),
                        "grad_year": str(refined.get("grad_year", "")),
                        "grade_desc": refined.get("grade_desc", ""),
                        "current_city": refined.get("current_city", ""),
                        "status_desc": "在线求职中",
                        "synced_at": ""
                    }
        except Exception:
            pass

    return {
        "name": "求职者",
        "avatar": "",
        "school": "",
        "major": "",
        "grad_year": "",
        "grade_desc": "",
        "current_city": "",
        "status_desc": "在线求职中",
        "synced_at": ""
    }


def fetch_boss_user_profile(cfg=None) -> dict:
    """通过 CDP 从当前 BOSS 真实登录会话中抓取真实个人资料（姓名、头像、学校、状态等）。"""
    cfg = cfg or cfgmod.load()
    cdp_http = cfg.get("cdp_endpoint", "http://127.0.0.1:9335")

    try:
        urlopen(cdp_http + "/json/version", timeout=2)
    except Exception:
        return get_cached_user_profile()

    name = ""
    avatar = ""
    school = ""
    major = ""
    grad_year = ""
    status_desc = ""

    extract_js = """
    (() => {
        let name = '';
        let avatar = '';
        let school = '';
        let major = '';
        let grad_year = '';
        let status_desc = '';

        const banner = document.querySelector('.userinfo-banner');
        if (banner) {
            name = (banner.querySelector('.username span') || {}).innerText || '';
            avatar = (banner.querySelector('.headbox img') || {}).src || '';
            const userinfoSpans = Array.from(banner.querySelectorAll('.userinfo span')).map(s => s.innerText.trim());
            const state = (banner.querySelector('.now-state .ui-select-selected-value') || {}).innerText || '';
            const expect = (banner.querySelector('.expect') || {}).innerText || '';
            const edu = (banner.querySelector('.edu') || {}).innerText || '';

            if (state) status_desc = state;
            for (let s of userinfoSpans) {
                if (s.includes('毕业')) grad_year = s;
                else if (/大专|本科|硕士|博士/.test(s)) {
                    // 这是学历，不作为学校名称
                } else if (!s.includes('岁')) {
                    school = s;
                }
            }
            // 尝试从在线简历模块提取精确高校与专业
            const eduItem = document.querySelector('.resume-education .name, .education-item .name, .resume-education');
            if (eduItem) {
                const eduText = eduItem.innerText || '';
                if (eduText.includes('大学') || eduText.includes('学院')) {
                    const matchSchool = eduText.match(/([\u4e00-\u9fa5]{2,12}(?:大学|学院))/);
                    if (matchSchool) school = matchSchool[1];
                }
            }
        }

        if (!name) {
            const navText = document.querySelector('.nav-figure .label-text, .nav-figure .name, .user-name, .header-username .label-text');
            if (navText) name = navText.innerText.trim();
        }
        if (!avatar) {
            const navImg = document.querySelector('.nav-figure img, .user-avatar img, .header-nav-figure img');
            if (navImg) avatar = navImg.src;
        }
        if (!name && window._PAGE && window._PAGE.name) {
            name = window._PAGE.name;
        }
        if (!avatar && window._PAGE && (window._PAGE.largeAvatar || window._PAGE.tinyAvatar)) {
            avatar = window._PAGE.largeAvatar || window._PAGE.tinyAvatar;
        }

        return JSON.stringify({
            name: name.trim(),
            avatar: avatar.trim(),
            school: school.trim(),
            major: major.trim(),
            grad_year: grad_year.trim(),
            status_desc: status_desc.trim()
        });
    })()
    """

    # 1. 优先从已有求职者会话 tab（geek/recommend, geek/chat, geek/jobs 等）提取
    try:
        req = urlopen(cdp_http + "/json", timeout=3)
        tabs = json.loads(req.read().decode("utf-8"))
        geek_tab = next((t for t in tabs if "zhipin.com/web/geek" in t.get("url", "") and t.get("webSocketDebuggerUrl")), None)
        if geek_tab:
            import websocket
            ws = websocket.create_connection(geek_tab["webSocketDebuggerUrl"], timeout=5)
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": extract_js, "returnByValue": True}}))
            raw_res = json.loads(ws.recv())
            val = raw_res.get("result", {}).get("result", {}).get("value")
            ws.close()
            if val:
                parsed = json.loads(val)
                name = parsed.get("name", "").strip()
                avatar = parsed.get("avatar", "").strip()
                school = parsed.get("school", "").strip()
                major = parsed.get("major", "").strip()
                grad_year = parsed.get("grad_year", "").strip()
                status_desc = parsed.get("status_desc", "").strip()
    except Exception:
        pass

    # 2. 若未提取完整，通过 RawCDP 在后台打开个人中心 /web/geek/recommend 抓取完整画像
    if not name or not school or not grad_year:
        sess = None
        try:
            sess = rawcdp.RawCDP(cdp_http)
            tab_id = sess.open_tab("https://www.zhipin.com/web/geek/recommend", background=True)
            time.sleep(2.5)
            raw_res = sess.eval(extract_js)
            sess.close_tab()

            if raw_res:
                parsed = json.loads(raw_res)
                if not name:
                    name = parsed.get("name", "").strip()
                if not avatar:
                    avatar = parsed.get("avatar", "").strip()
                if not school and parsed.get("school"):
                    school = parsed.get("school", "").strip()
                if not major and parsed.get("major"):
                    major = parsed.get("major", "").strip()
                if not grad_year:
                    grad_year = parsed.get("grad_year", "").strip()
                if not status_desc:
                    status_desc = parsed.get("status_desc", "").strip()
        except Exception:
            pass
        finally:
            if sess:
                try:
                    sess.close()
                except Exception:
                    pass

    # 3. 严格过滤非登录状态占位符（仅过滤未登录提示文本）
    if name in ("登录/注册", "注册/登录", "登录", "注册", "立即登录", "请登录"):
        name = ""

    cached = get_cached_user_profile() or {}
    real_name = name or cached.get("name") or "求职者"

    # 保护真实院校与专业：若抓取到的 school 包含学历词或为空，保留 cached
    final_school = school if (school and not re.match(r"^(本科|硕士|大专|博士|学历)$", school)) else (cached.get("school") or "")
    # 保护真实专业：若抓取到的 major 含薪资特征(K/k/元)或为空，保留 cached
    final_major = major if (major and not re.search(r"\d+[-~]\d+[kK元]|期望", major)) else (cached.get("major") or "")

    grade = "离校" if "离校" in status_desc else ("在校" if "在校" in status_desc else cached.get("grade_desc", "在读"))
    profile_res = {
        "name": real_name,
        "avatar": avatar or cached.get("avatar") or "",
        "school": final_school,
        "major": final_major,
        "grad_year": grad_year or cached.get("grad_year", ""),
        "grade_desc": grade,
        "current_city": cached.get("current_city", ""),
        "status_desc": status_desc or cached.get("status_desc", ""),
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    # 缓存到 state/user_profile.json
    cfgmod.atomic_save_json(USER_PROFILE_CACHE, profile_res)
    return profile_res


def sync_profile_to_local(profile_data: dict) -> dict:
    """将从 BOSS 获取的真实资料增量合并写入 profile.local.json。"""
    p_path = profile_store.PROFILE_PATH
    local_data = {}
    if os.path.exists(p_path):
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                local_data = json.load(f)
        except Exception:
            local_data = {}

    refined = local_data.get("refined") or {}
    r_name = profile_data.get("name")
    if r_name and r_name not in ("登录/注册", "注册/登录", "登录", "注册"):
        refined["name"] = r_name

    if profile_data.get("school"):
        refined["school"] = profile_data["school"]
    if profile_data.get("major"):
        refined["major"] = profile_data["major"]
    if profile_data.get("grad_year"):
        try:
            yr = int(re.sub(r"\D", "", str(profile_data["grad_year"])))
            if yr:
                refined["grad_year"] = yr
        except Exception:
            pass
    refined["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    local_data["refined"] = refined

    cfgmod.atomic_save_json(p_path, local_data)
    cfgmod.atomic_save_json(USER_PROFILE_CACHE, profile_data)
    return profile_data


class QRLoginManager:
    """扫码鉴权与 Cookie 会话热注入管理器。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(QRLoginManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._sessions = {}  # uuid -> dict
        self._initialized = True

    def _cdp_endpoint(self, cfg=None):
        cfg = cfg or cfgmod.load()
        return cfg.get("cdp_endpoint", "http://127.0.0.1:9335")

    def _cleanup_session(self, uuid: str):
        with self._lock:
            sess_info = self._sessions.pop(uuid, None)
        if not sess_info:
            return
        try:
            sess = sess_info.get("sess")
            ctx_id = sess_info.get("ctx_id")
            tab_id = sess_info.get("tab_id")
            if sess:
                if tab_id:
                    try:
                        sess._send("Target.closeTarget", {"targetId": tab_id})
                    except Exception:
                        pass
                if ctx_id:
                    try:
                        sess._send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                    except Exception:
                        pass
                sess.close()
        except Exception:
            pass

    def get_qrcode(self, cfg=None) -> dict:
        """导航至登录页，捕获原生二维码 Base64 与 UUID。"""
        cfg = cfg or cfgmod.load()
        cdp_http = self._cdp_endpoint(cfg)

        # 1. 验证 CDP 连通性 (若未启动则自动后台静默拉起)
        if not ensure_chrome_running(cfg):
            return {
                "ok": False,
                "error": "无法连接或拉起调试 Chrome CDP (端口 9335)。请确认系统已安装 Chrome 浏览器。"
            }

        # 2. 清理旧会话
        with self._lock:
            old_uuids = list(self._sessions.keys())
        for old_id in old_uuids:
            self._cleanup_session(old_id)

        # 3. 建立专用 RawCDP 连接并在隔离 Context 下打开登录页
        sess = None
        ctx_id = None
        tab_id = None
        sid = None
        try:
            sess = rawcdp.RawCDP(cdp_http)
            b_ctx = sess._send("Target.createBrowserContext")
            ctx_id = b_ctx["browserContextId"]
            tab = sess._send("Target.createTarget", {
                "url": LOGIN_URL,
                "browserContextId": ctx_id,
                "background": True
            })
            tab_id = tab["targetId"]
            sid = sess._send("Target.attachToTarget", {"targetId": tab_id, "flatten": True})["sessionId"]
            sess.sid = sid
            sess._send("Network.enable", sid=sid)
            sess.events = []

            # 4. 轮询 DOM 渲染并触发切换到二维码登录界面
            deadline = time.time() + 15
            qr_clicked = False
            b64_img = None
            uuid = None

            while time.time() < deadline:
                # 检查是否需要点击切换到二维码
                if not qr_clicked:
                    click_res = sess._send("Runtime.evaluate", {
                        "expression": """
                        (() => {
                          const btn = document.querySelector('.btn-sign-switch');
                          if (btn && !btn.className.includes('phone-switch')) {
                            btn.click();
                            return true;
                          }
                          return !!document.querySelector('.qr-code-box, .qr-img-box');
                        })()
                        """,
                        "returnByValue": True
                    }, sid=sid)
                    if click_res.get("result", {}).get("value"):
                        qr_clicked = True

                # 尝试从网络事件中捕获 UUID
                sess.drain_events(0.6)
                for ev in sess.events:
                    if ev.get("sessionId") == sid and ev.get("method") == "Network.requestWillBeSent":
                        req_url = ev.get("params", {}).get("request", {}).get("url", "")
                        m = re.search(r"uuid=(bosszp-[a-zA-Z0-9\-]+)", req_url)
                        if m:
                            uuid = m.group(1)
                            break

                # 尝试抓取二维码图片 Base64
                if qr_clicked:
                    img_res = sess._send("Runtime.evaluate", {
                        "expression": """
                        (async () => {
                          const img = document.querySelector('.qr-img-box img, .qr-code-box img, [class*=qr-img] img');
                          if (!img || !img.src) return null;
                          try {
                            const r = await fetch(img.src);
                            const b = await r.blob();
                            return await new Promise((resolve) => {
                              const reader = new FileReader();
                              reader.onloadend = () => resolve(reader.result);
                              reader.readAsDataURL(b);
                            });
                          } catch (e) {
                            return null;
                          }
                        })()
                        """,
                        "returnByValue": True,
                        "awaitPromise": True
                    }, sid=sid)
                    b64_val = img_res.get("result", {}).get("value")
                    if b64_val and b64_val.startswith("data:image"):
                        b64_img = b64_val

                if uuid and b64_img:
                    break
                time.sleep(0.4)

            # 兜底：如果 Network 没抓到 UUID，尝试从页面上下文提取或生成稳定标识
            if not uuid:
                eval_uuid = sess._send("Runtime.evaluate", {
                    "expression": """
                    (() => {
                      let u = '';
                      try {
                        const root = document.querySelector('#app') || document.querySelector('.page-sign');
                        if (root && root.__vue__ && root.__vue__.uuid) u = root.__vue__.uuid;
                      } catch(e){}
                      return u;
                    })()
                    """,
                    "returnByValue": True
                }, sid=sid).get("result", {}).get("value")
                if eval_uuid:
                    uuid = eval_uuid

            if not b64_img:
                raise RuntimeError("未能从登录页捕获二维码图片数据")
            if not uuid:
                # 兜底 UUID
                uuid = f"bosszp-auto-{int(time.time()*1000)}"

            with self._lock:
                self._sessions[uuid] = {
                    "sess": sess,
                    "ctx_id": ctx_id,
                    "tab_id": tab_id,
                    "sid": sid,
                    "created_at": time.time(),
                    "expire_seconds": 180,
                    "qrcode_base64": b64_img,
                }

            return {
                "ok": True,
                "uuid": uuid,
                "qrcode_base64": b64_img,
                "expire_seconds": 180
            }

        except Exception as e:
            if sess:
                try:
                    if tab_id:
                        sess._send("Target.closeTarget", {"targetId": tab_id})
                    if ctx_id:
                        sess._send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                    sess.close()
                except Exception:
                    pass
            return {
                "ok": False,
                "error": f"生成二维码失败: {str(e)}"
            }

    def check_scan_status(self, uuid: str, cfg=None) -> dict:
        """检查二维码扫描状态并处理成功时的凭证热注入。

        返回: {"status": "waiting" | "scanned" | "confirmed" | "expired", "message": "..."}
        """
        cfg = cfg or cfgmod.load()
        with self._lock:
            sess_info = self._sessions.get(uuid)

        if not sess_info:
            # 会话不存在或已被处理：检查主 CDP 是否已经处于已登录态
            auth = self.get_auth_status(cfg)
            if auth.get("logged_in"):
                return {"status": "confirmed", "message": "已处于有效登录态", "auth": auth}
            return {"status": "expired", "message": "扫码会话已过期或不存在"}

        # 检查是否已超过 180 秒过期
        age = time.time() - sess_info.get("created_at", 0)
        if age > sess_info.get("expire_seconds", 180):
            self._cleanup_session(uuid)
            return {"status": "expired", "message": "二维码已过期，请点击刷新重新获取"}

        sess = sess_info["sess"]
        sid = sess_info["sid"]

        try:
            # 1. 检查隔离会话标签页中的 cookies
            ck_resp = sess._send("Network.getCookies", sid=sid)
            cookies = ck_resp.get("cookies", [])
            has_wt2 = any(c.get("name") == "wt2" and c.get("value") for c in cookies)

            # 2. 检查 DOM 状态
            dom_st = sess._send("Runtime.evaluate", {
                "expression": """
                (() => {
                  const text = document.body ? document.body.innerText : '';
                  const hasMask = !!document.querySelector('.qrcode-mask, .refresh-qrcode, [class*=mask]');
                  const isExp = hasMask || /二维码已失效|二维码已过期|点击刷新/.test(text);
                  const isScanned = /请在.*(?:App|手机).*确认|扫描成功|确认登录/.test(text) || !!document.querySelector('.scan-help-wrapper:not([style*="display: none"])');
                  const avatar = !!document.querySelector('.nav-figure, .header-nav-figure');
                  const redirected = !location.href.includes('/web/user');
                  return {
                    expired: isExp,
                    scanned: isScanned,
                    confirmed: avatar || redirected,
                    url: location.href
                  };
                })()
                """,
                "returnByValue": True
            }, sid=sid).get("result", {}).get("value") or {}

            # 3. 判定登录成功
            if has_wt2 or dom_st.get("confirmed"):
                # 获取该上下文的所有 zhipin cookies
                zp_cookies = [c for c in cookies if "zhipin.com" in c.get("domain", "")]
                if not zp_cookies:
                    time.sleep(0.5)
                    ck_resp = sess._send("Network.getCookies", sid=sid)
                    zp_cookies = [c for c in ck_resp.get("cookies", []) if "zhipin.com" in c.get("domain", "")]

                # DPAPI 加密持久化落盘
                if zp_cookies:
                    secrets_mod.set_secret(COOKIE_SECRET_KEY, json.dumps(zp_cookies, ensure_ascii=False))

                # 原子化热注入主 Chrome CDP
                injected = self.inject_cookies_to_cdp(zp_cookies, cfg)

                # 抓取并同步用户个人真实资料
                user_prof = {}
                try:
                    user_prof = fetch_boss_user_profile(cfg)
                    sync_profile_to_local(user_prof)
                except Exception:
                    user_prof = get_cached_user_profile()

                # 清理临时隔离会话
                self._cleanup_session(uuid)

                return {
                    "status": "confirmed",
                    "message": "扫码登录成功！已自动同步画像并热注入会话凭证。",
                    "cookies_count": len(zp_cookies),
                    "injected": injected,
                    "user_profile": user_prof or get_cached_user_profile()
                }

            # 4. 判定手机已扫码待确认
            if dom_st.get("scanned"):
                return {
                    "status": "scanned",
                    "message": "手机已成功扫码，请在 BOSS直聘 App 点击【确认登录】..."
                }

            # 5. 判定过期
            if dom_st.get("expired"):
                self._cleanup_session(uuid)
                return {
                    "status": "expired",
                    "message": "二维码已失效，请重新刷新获取"
                }

            # 6. 等待扫码
            return {
                "status": "waiting",
                "message": "等待用户使用 BOSS直聘 App 扫码...",
                "remain_seconds": max(0, int(sess_info.get("expire_seconds", 180) - age))
            }

        except Exception as e:
            # 若页面突然崩溃或连接异常
            return {"status": "waiting", "message": f"状态检测中: {str(e)[:80]}"}

    def inject_cookies_to_cdp(self, cookies: list, cfg=None) -> bool:
        """通过 CDP Network.setCookies / Storage.setCookies 原子化注入 Chrome 实例。"""
        if not cookies:
            return False
        cfg = cfg or cfgmod.load()
        cdp_http = self._cdp_endpoint(cfg)

        cleaned = []
        for c in cookies:
            entry = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".zhipin.com"),
                "path": c.get("path", "/"),
            }
            if "secure" in c:
                entry["secure"] = bool(c["secure"])
            if "httpOnly" in c:
                entry["httpOnly"] = bool(c["httpOnly"])
            if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
                entry["sameSite"] = c["sameSite"]
            if "expires" in c and c["expires"] is not None:
                entry["expires"] = float(c["expires"])
            cleaned.append(entry)

        try:
            sess = rawcdp.RawCDP(cdp_http)
            # 1. 优先尝试 Storage.setCookies 浏览器级生效
            try:
                sess._send("Storage.setCookies", {"cookies": cleaned})
            except Exception:
                pass

            # 2. 开一个临时标签页调用 Network.setCookies
            tab_id = sess.open_tab()
            try:
                sess._send("Network.setCookies", {"cookies": cleaned}, sid=sess.sid)
            finally:
                sess.close_tab()
            sess.close()
            return True
        except Exception:
            return False

    def get_auth_status(self, cfg=None) -> dict:
        """轻量检测当前 Chrome 或持久化 Cookie 是否处于有效登录态。"""
        cfg = cfg or cfgmod.load()
        cdp_http = self._cdp_endpoint(cfg)
        has_persisted = secrets_mod.has_secret(COOKIE_SECRET_KEY)

        # 检查 CDP 连通性
        cdp_alive = False
        try:
            urlopen(cdp_http + "/json/version", timeout=2)
            cdp_alive = True
        except Exception:
            cdp_alive = False

        if not cdp_alive:
            return {
                "cdp_connected": False,
                "logged_in": False,
                "has_persisted": has_persisted,
                "cookies_count": 0,
                "message": "Chrome 调试端口 (9335) 未连接"
            }

        # 检查主 Chrome CDP 中的 Cookie
        wt2_val = None
        cookies_count = 0
        try:
            sess = rawcdp.RawCDP(cdp_http)
            res = sess._send("Storage.getCookies")
            sess.close()
            all_ck = res.get("cookies", [])
            zp_ck = [c for c in all_ck if "zhipin.com" in c.get("domain", "")]
            cookies_count = len(zp_ck)
            for c in zp_ck:
                if c.get("name") == "wt2":
                    wt2_val = c.get("value")
                    break
        except Exception:
            pass

        # 若 Chrome 内存中没有 wt2，但本地有 DPAPI 持久化的 Cookie，尝试一键自愈热注入
        if not wt2_val and has_persisted:
            try:
                persisted_str = secrets_mod.get_secret(COOKIE_SECRET_KEY)
                if persisted_str:
                    persisted_cookies = json.loads(persisted_str)
                    if any(c.get("name") == "wt2" for c in persisted_cookies):
                        self.inject_cookies_to_cdp(persisted_cookies, cfg)
                        for c in persisted_cookies:
                            if c.get("name") == "wt2":
                                wt2_val = c.get("value")
                                break
                        cookies_count = len(persisted_cookies)
            except Exception:
                pass

        logged_in = bool(wt2_val)
        return {
            "cdp_connected": True,
            "logged_in": logged_in,
            "has_persisted": has_persisted,
            "cookies_count": cookies_count,
            "wt2_masked": secrets_mod.masked(wt2_val) if wt2_val else "",
            "message": "已登录 (凭证正常)" if logged_in else "未登录 (需扫码接入)",
            "user_profile": get_cached_user_profile() if logged_in else None
        }
