# -*- coding: utf-8 -*-
"""P0 级核心功能集成自动化验证脚本。
覆盖：
1. 岗位预设模板解耦 (presets.py)
2. 打分引擎角色门禁放行与加分 (scorer.py)
3. 沟通心法姿态解耦 (ai_reply.py)
4. Web 端开箱向导 API 链路 (/api/presets, /api/wizard/status, /api/wizard/apply)
"""
import sys
import os
import unittest
from fastapi.testclient import TestClient

# 注入 worktree 根路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boss_apply import presets, scorer, ai_reply, config as cfgmod
from scripts.approval_web import app


class TestPresetsAndScorer(unittest.TestCase):
    def setUp(self):
        self.base_cfg = cfgmod.load()

    def test_presets_list(self):
        all_p = presets.list_presets()
        self.assertEqual(len(all_p), 4)
        p_ids = [p["id"] for p in all_p]
        self.assertIn("ai_pm", p_ids)
        self.assertIn("tech_dev", p_ids)
        self.assertIn("sales_bd", p_ids)
        self.assertIn("general_ops", p_ids)

    def test_tech_dev_unblocks_tech_roles(self):
        # 验证在技术研发包下，前后端与算法不再被误杀
        tech_cfg = dict(self.base_cfg, preset="tech_dev")
        
        backend_job = {
            "title": "Java后端开发工程师",
            "company": "阿里巴巴",
            "salary": "25-35K",
            "boss_active": 1,
            "tags": "Java Spring 微服务 MySQL"
        }
        ok, reason = scorer.check_eligibility(backend_job, "", tech_cfg)
        self.assertTrue(ok, f"后端岗在技术包中不应被门禁拦截: {reason}")
        
        score, score_reasons = scorer.calculate_matching_score(backend_job, "精通 Java, Spring Boot, MySQL", tech_cfg)
        self.assertGreaterEqual(score, 8, f"后端岗在技术包中应获得匹配高分: {score_reasons}")
        self.assertIn("+2 tech", score_reasons)

        algo_job = {
            "title": "大模型算法工程师",
            "company": "深度求索",
            "salary": "30-50K",
            "boss_active": 2,
            "tags": "Python PyTorch LLM Agent"
        }
        ok_algo, reason_algo = scorer.check_eligibility(algo_job, "", tech_cfg)
        self.assertTrue(ok_algo, f"算法岗在技术包中不应被门禁拦截: {reason_algo}")

    def test_sales_bd_unblocks_sales_roles(self):
        # 验证在商务销售包下，销售与BD不再被误杀
        sales_cfg = dict(self.base_cfg, preset="sales_bd")
        
        bd_job = {
            "title": "大客户商务经理 (KA/BD)",
            "company": "字节跳动",
            "salary": "20-30K",
            "boss_active": 1,
            "tags": "ToB 客户开拓 商务谈判"
        }
        ok, reason = scorer.check_eligibility(bd_job, "", sales_cfg)
        self.assertTrue(ok, f"销售BD岗在销售包中不应被门禁拦截: {reason}")
        
        score, score_reasons = scorer.calculate_matching_score(bd_job, "负责大客户商务拓展与KA客情维护", sales_cfg)
        self.assertGreaterEqual(score, 8, f"商务岗在销售包中应获得匹配高分: {score_reasons}")
        self.assertIn("+2 sales", score_reasons)

    def test_ai_pm_preserves_guardrails(self):
        # 验证在 AI/PM 模式下，销售/地推与纯开发仍被原有规则严格门禁保护
        pm_cfg = dict(self.base_cfg, preset="ai_pm")
        
        sales_job = {"title": "电话销售专员", "company": "某电销公司", "salary": "5-8K"}
        ok, reason = scorer.check_eligibility(sales_job, "", pm_cfg)
        self.assertFalse(ok)
        self.assertIn("kill: title role-gate", reason)

    def test_ai_reply_dynamic_stances(self):
        tech_cfg = dict(self.base_cfg, preset="tech_dev")
        eng_tech = ai_reply.AIReplyEngine(tech_cfg)
        prompt_tech = eng_tech.build_agent_prompt({"who": "HR", "last_msg": "你好"})
        self.assertIn("技术栈契合度", prompt_tech)
        self.assertIn("见人下菜碟", prompt_tech)
        self.assertIn("三不原则", prompt_tech)

        sales_cfg = dict(self.base_cfg, preset="sales_bd")
        eng_sales = ai_reply.AIReplyEngine(sales_cfg)
        prompt_sales = eng_sales.build_agent_prompt({"who": "HR", "last_msg": "你好"})
        self.assertIn("业务拓展与客户链接能力", prompt_sales)


class TestWizardAPIs(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.token = "boss-apply"

    def test_api_presets(self):
        res = self.client.get(f"/api/presets?token={self.token}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("presets", data)
        self.assertEqual(len(data["presets"]), 4)

    def test_api_wizard_status(self):
        res = self.client.get(f"/api/wizard/status?token={self.token}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("wizard_completed", data)
        self.assertIn("should_prompt", data)
        self.assertIn("presets", data)

    def test_api_wizard_apply(self):
        payload = {
            "preset": "tech_dev",
            "cities": ["杭州", "上海", "深圳"],
            "llm": {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V3",
                "api_key": "sk-test-mock-key"
            }
        }
        res = self.client.post(f"/api/wizard/apply?token={self.token}", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("active_preset"), "tech_dev")

        # 验证 config.local.json 是否更新
        cfg_loaded = cfgmod.load()
        self.assertEqual(cfg_loaded.get("preset"), "tech_dev")
        self.assertTrue(cfg_loaded.get("wizard_completed"))
        prefs = cfg_loaded.get("prefs") or {}
        self.assertEqual(prefs.get("want_cities"), ["杭州", "上海", "深圳"])

    def tearDown(self):
        # 恢复初始状态，避免影响其他全局测试
        import shutil
        if os.path.exists(cfgmod.LOCAL_CFG_PATH):
            example_cfg = os.path.join(ROOT, "config.local.example.json")
            if os.path.exists(example_cfg):
                shutil.copy2(example_cfg, cfgmod.LOCAL_CFG_PATH)
            else:
                os.remove(cfgmod.LOCAL_CFG_PATH)


class TestOneClickImport(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.token = "boss-apply"

    def test_parse_cc_switch_single_json(self):
        from scripts.approval_web import parse_api_key_config
        sample = '''{
            "name": "SiliconFlow",
            "baseUrl": "https://api.siliconflow.cn/v1",
            "apiKey": "sk-test-siliconflow-key-12345678",
            "model": "deepseek-ai/DeepSeek-V3"
        }'''
        res = parse_api_key_config(sample)
        self.assertTrue(res["ok"])
        self.assertEqual(res["provider"], "siliconflow")
        self.assertEqual(res["base_url"], "https://api.siliconflow.cn/v1")
        self.assertEqual(res["api_key"], "sk-test-siliconflow-key-12345678")
        self.assertEqual(res["model"], "deepseek-ai/DeepSeek-V3")

    def test_parse_cc_switch_multi_export(self):
        from scripts.approval_web import parse_api_key_config
        sample = '''{
            "providers": [
                {
                    "name": "DeepSeek 官方",
                    "baseUrl": "https://api.deepseek.com",
                    "apiKey": "sk-deepseek-official-key-98765432",
                    "model": "deepseek-chat"
                }
            ]
        }'''
        res = parse_api_key_config(sample)
        self.assertTrue(res["ok"])
        self.assertEqual(res["provider"], "deepseek")
        self.assertEqual(res["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(res["api_key"], "sk-deepseek-official-key-98765432")
        self.assertEqual(res["model"], "deepseek-chat")

    def test_parse_cherry_studio_json(self):
        from scripts.approval_web import parse_api_key_config
        sample = '''{
            "id": "deepseek",
            "name": "DeepSeek",
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKey": "sk-cherry-deepseek-key-1234567890",
            "models": [{"id": "deepseek-chat", "name": "DeepSeek V3"}]
        }'''
        res = parse_api_key_config(sample)
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "deepseek-chat")
        self.assertEqual(res["api_key"], "sk-cherry-deepseek-key-1234567890")

    def test_parse_shell_export(self):
        from scripts.approval_web import parse_api_key_config
        sample = '''
        export OPENAI_BASE_URL="https://api.siliconflow.cn/v1"
        export OPENAI_API_KEY="sk-env-test-key-1234567890abcdef"
        export OPENAI_MODEL="deepseek-ai/DeepSeek-V3"
        '''
        res = parse_api_key_config(sample)
        self.assertTrue(res["ok"])
        self.assertEqual(res["base_url"], "https://api.siliconflow.cn/v1")
        self.assertEqual(res["api_key"], "sk-env-test-key-1234567890abcdef")

    def test_parse_curl_command(self):
        from scripts.approval_web import parse_api_key_config
        sample = 'curl https://api.deepseek.com/v1/chat/completions -H "Authorization: Bearer sk-curl-test-key-1234567890" -d \'{"model": "deepseek-chat"}\''
        res = parse_api_key_config(sample)
        self.assertTrue(res["ok"])
        self.assertEqual(res["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(res["api_key"], "sk-curl-test-key-1234567890")
        self.assertEqual(res["model"], "deepseek-chat")

    def test_normalize_base_url_strips_chat_completions(self):
        from scripts.approval_web import normalize_base_url
        raw = "https://api.deepseek.com/v1/chat/completions"
        self.assertEqual(normalize_base_url(raw), "https://api.deepseek.com/v1")

        raw_models = "https://api.siliconflow.cn/v1/models"
        self.assertEqual(normalize_base_url(raw_models), "https://api.siliconflow.cn/v1")

    def test_api_import_key_endpoint(self):
        sample = '''{
            "baseUrl": "https://api.siliconflow.cn/v1/chat/completions",
            "apiKey": "sk-endpoint-test-key-1234567890",
            "model": "deepseek-ai/DeepSeek-V3"
        }'''
        res = self.client.post(f"/api/settings/import_key?token={self.token}", json={"raw_text": sample})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("provider"), "siliconflow")
        self.assertEqual(data.get("base_url"), "https://api.siliconflow.cn/v1")
        self.assertEqual(data.get("model"), "deepseek-ai/DeepSeek-V3")
        self.assertIn("api_key_masked", data)


if __name__ == "__main__":
    unittest.main()

