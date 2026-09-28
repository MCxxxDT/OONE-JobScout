# -*- coding: utf-8 -*-
"""大模型连通性与透明化诊断自动化回归测试。
验证内容：
1. GET /api/llm/status 诊断接口结构与健康状态
2. POST /api/settings/test 支持空 body 测试已存密钥
3. POST /api/playground/simulate 大模型异常时明确返回 rule_fallback 与错误原因，杜绝误标 LLM 直通
4. Web 前端 UI 组件（TopBar 状态胶囊、已存密钥卡片、一键测速按钮、Playground 警示条）渲染完整性
"""
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.approval_web import (
    app,
    _format_llm_error,
    _detect_provider_name,
)
from boss_apply import secrets as secrets_mod, config as cfgmod


class TestLLMTransparency(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.token = "boss-apply"
        
        # 备份真实配置和密钥
        self.secrets_path = secrets_mod.SECRETS_PATH
        self.secrets_backup = None
        if os.path.exists(self.secrets_path):
            with open(self.secrets_path, "rb") as f:
                self.secrets_backup = f.read()

        self.cfg_backup = None
        if os.path.exists(cfgmod.LOCAL_CFG_PATH):
            with open(cfgmod.LOCAL_CFG_PATH, "rb") as f:
                self.cfg_backup = f.read()

    def tearDown(self):
        # 严苛恢复初始状态，确保不污染开发者的真实凭据
        if self.secrets_backup is not None:
            with open(self.secrets_path, "wb") as f:
                f.write(self.secrets_backup)
        elif os.path.exists(self.secrets_path):
            os.remove(self.secrets_path)

        if self.cfg_backup is not None:
            with open(cfgmod.LOCAL_CFG_PATH, "wb") as f:
                f.write(self.cfg_backup)
        elif os.path.exists(cfgmod.LOCAL_CFG_PATH):
            os.remove(cfgmod.LOCAL_CFG_PATH)

    def test_format_llm_error(self):
        # 401 密钥失效
        f401 = _format_llm_error("HTTP 401: Authentication Fails")
        self.assertIn("401", f401)
        self.assertIn("API Key 无效", f401)

        # 429 配额用尽 / 限流
        f429 = _format_llm_error("HTTP 429: Rate limit exceeded or quota exhausted")
        self.assertIn("429", f429)
        self.assertIn("访问受限", f429)

        # 404 模型不存在
        f404 = _format_llm_error("HTTP 404: Model not found")
        self.assertIn("404", f404)

        # 500/503 服务端异常
        f500 = _format_llm_error("HTTP 503: Service Unavailable")
        self.assertIn("5xx", f500)

        # 超时
        ftimeout = _format_llm_error("ConnectTimeout: timed out after 10s")
        self.assertIn("超时", ftimeout)

    def test_detect_provider_name(self):
        self.assertEqual(_detect_provider_name("https://api.deepseek.com/v1"), "DeepSeek 官方")
        self.assertEqual(_detect_provider_name("https://api.siliconflow.cn/v1"), "硅基流动 (SiliconFlow)")
        self.assertEqual(_detect_provider_name("https://open.bigmodel.cn/api/paas/v4"), "智谱 GLM-4")
        self.assertEqual(_detect_provider_name("https://dashscope.aliyuncs.com/compatible-mode/v1"), "阿里通义千问")
        self.assertEqual(_detect_provider_name("https://ark.cn-beijing.volces.com/api/v3"), "字节火山引擎方舟")
        self.assertEqual(_detect_provider_name("https://api.openai.com/v1"), "OpenAI 官方")
        self.assertEqual(_detect_provider_name("http://127.0.0.1:11434/v1"), "本地 Ollama")
        self.assertEqual(_detect_provider_name("https://my-custom-proxy.example.com/v1"), "自定义渠道")

    def test_api_llm_status_endpoint(self):
        res = self.client.get(f"/api/llm/status?token={self.token}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("configured", data)
        self.assertIn("status", data)
        self.assertIn("provider", data)
        self.assertIn("model", data)
        self.assertIn("base_url", data)
        self.assertIn("api_key_masked", data)
        self.assertIn("key_source", data)
        self.assertIn("latency_ms", data)
        self.assertIn("last_tested_at", data)

    def test_api_settings_test_empty_body(self):
        # 写入一个明确的临时测试密钥，验证空 body 能够自动读取已存密钥
        secrets_mod.set_secret("llm_api_key", "sk-mock-testing-transparency-123456")
        
        # 模拟外部调用以避免产生真实网络请求
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = self.client.post(f"/api/settings/test?token={self.token}", json={})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("latency_ms", data)

    def test_playground_simulate_fallback_transparency(self):
        # 写入一个临时密钥使 has_key 为 True，然后 mock 网络抛出 401 鉴权异常
        secrets_mod.set_secret("llm_api_key", "sk-mock-key-for-test-fallback")

        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = RuntimeError("HTTP 401: Authentication Fails (Key Expired)")
            
            payload = {
                "message": "你是谁？",
                "chat_history": []
            }
            res = self.client.post(f"/api/playground/simulate?token={self.token}", json=payload)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            # 当底层大模型失败时，llm_source 必须明确为 rule_fallback，杜绝冒充 LLM 直通
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("llm_source"), "rule_fallback")
            self.assertIn("401", data.get("fallback_reason", ""))
            self.assertIn("401", data.get("llm_error", ""))
            self.assertIn("cleaned_reply", data)
            self.assertTrue(len(data.get("cleaned_reply", "")) > 0)

    def test_html_ui_components_rendered(self):
        res = self.client.get(f"/?token={self.token}")
        self.assertEqual(res.status_code, 200)
        html = res.text

        # 1. 顶栏 LLM 实时状态胶囊与呼吸灯
        self.assertIn('id="btnTopLLMStatus"', html)
        self.assertIn('id="topLLMDot"', html)
        self.assertIn('id="topLLMText"', html)
        
        # 2. 设置弹窗已存密钥卡片与独立测速按钮
        self.assertIn('id="activeLLMCard"', html)
        self.assertIn('id="activeLLMProvider"', html)
        self.assertIn('id="activeLLMMaskedKey"', html)
        self.assertIn('id="btnTestSavedKey"', html)
        self.assertIn('testSavedLLM()', html)
        self.assertIn('toggleLLMReplaceSection()', html)
        
        # 3. 设置中心卡片 1 中的测速与状态徽章
        self.assertIn('id="hubBadgeLLMStatus"', html)
        
        # 4. 演练场模型状态条与提示
        self.assertIn('id="pgLLMStrip"', html)
        self.assertIn('id="pgLLMBadge"', html)
        self.assertIn('id="pgLLMDetail"', html)


if __name__ == "__main__":
    unittest.main()
