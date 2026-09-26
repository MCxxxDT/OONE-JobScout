"""自动化实机验证脚本：启动加速、启动动效进场动画与 BOSS 浏览器隐形运行。"""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient
from scripts.approval_web import app
from boss_apply import config as cfgmod

class TestSplashAndSilentBrowser(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_default_config_silent_mode(self):
        """验证示例配置与本地覆盖层均默认开启静默隐形守护模式。"""
        ex_path = os.path.join(_ROOT, "config.local.example.json")
        self.assertTrue(os.path.exists(ex_path))
        with open(ex_path, "r", encoding="utf-8") as f:
            ex_data = json.load(f)
        self.assertTrue(ex_data.get("browser", {}).get("silent_mode", False), "config.local.example.json 必须默认开启 silent_mode")
        self.assertTrue(ex_data.get("browser", {}).get("minimize_on_start", False), "config.local.example.json 必须默认开启 minimize_on_start")

        local_path = os.path.join(_ROOT, "config.local.json")
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                local_data = json.load(f)
            self.assertTrue(local_data.get("browser", {}).get("silent_mode", False), "config.local.json 必须默认开启 silent_mode")

    def test_page_contains_splash_screen(self):
        """验证工作台前端 HTML 完整包含高逼格开机启动动画与进度条组件。"""
        resp = self.client.get("/?token=boss-apply")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("id=\"appSplashScreen\"", html, "页面必须包含 #appSplashScreen 容器")
        self.assertIn("splash-progress-bar", html, "页面必须包含启动动效进度条类")
        self.assertIn("id=\"splashBar\"", html, "页面必须包含 #splashBar 进度条元素")
        self.assertIn("id=\"splashStepText\"", html, "页面必须包含 #splashStepText 步骤文案元素")
        self.assertIn("bootWorkbenchWithSplash()", html, "页面必须通过 bootWorkbenchWithSplash 驱动进场动效与预加载")
        self.assertIn("id=\"btnQuickToggleBrowser\"", html, "顶部导航栏必须包含 #btnQuickToggleBrowser 浏览器一键前台/隐形切换按钮")

    def test_api_overview_includes_browser_state(self):
        """验证 /api/overview 接口返回包含浏览器运行模式状态。"""
        resp = self.client.get("/api/overview?token=boss-apply")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("browser", data, "overview 数据必须包含 browser 状态字段")
        self.assertIn("silent_mode", data["browser"], "browser 字典必须包含 silent_mode")

    def test_api_browser_visibility_toggle(self):
        """验证 /api/browser/visibility 接口可无缝切换 normal 与 hide 模式。"""
        # 测试切换至 hide
        resp_hide = self.client.post("/api/browser/visibility?token=boss-apply", json={"mode": "hide"})
        self.assertEqual(resp_hide.status_code, 200)
        
        # 测试切换至 normal
        resp_norm = self.client.post("/api/browser/visibility?token=boss-apply", json={"mode": "normal"})
        self.assertEqual(resp_norm.status_code, 200)

        # 恢复默认 hide 保持环境干净
        self.client.post("/api/browser/visibility?token=boss-apply", json={"mode": "hide"})

if __name__ == "__main__":
    unittest.main()
