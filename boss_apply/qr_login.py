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
import threading
import time
from urllib.request import urlopen

from . import config as cfgmod, rawcdp, secrets as secrets_mod

LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
COOKIE_SECRET_KEY = "boss_session_cookies"


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

        # 1. 验证 CDP 连通性
        try:
            urlopen(cdp_http + "/json/version", timeout=3)
        except Exception as e:
            return {
                "ok": False,
                "error": f"无法连接调试 Chrome CDP (端口 9335): {e}。请确认已运行 launch_debug_chrome.bat。"
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

                # 清理临时隔离会话
                self._cleanup_session(uuid)

                return {
                    "status": "confirmed",
                    "message": "扫码登录成功！Session凭证已持久化并热注入 Chrome 实例。",
                    "cookies_count": len(zp_cookies),
                    "injected": injected
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
            "message": "已登录 (凭证正常)" if logged_in else "未登录 (需扫码接入)"
        }
