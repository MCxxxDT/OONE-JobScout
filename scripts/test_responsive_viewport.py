#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_responsive_viewport.py
----------------------------------------------------------------------
自动化响应式与视口高度适配专项测试 (Responsive Viewport & Overflow Audit)
验证在各种分辨率（1080p 125%/150% 缩放、1366x768、移动端）下：
1. 回复演练场 (.chat-arena-card) 绝无固定死高度 (820px)，采用 calc(100vh - 108px) 满屏自适应
2. 聊天流容器 (.chat-flow-container) 具备 min-height: 0，杜绝 Flexbox 溢出撑爆
3. 演练场输入框容器 (.chat-input-wrapper) 具备 flex-shrink: 0，确保在视口底部稳固展现杜绝滚轮翻页
4. 所有全局弹窗 (.modal-card, .setting-modal-card, .login-gate-card, .splash-card) 具备 max-height: calc(100vh - ...) 与 overflow-y: auto 保护
5. 浮层蒙版 (.modal-overlay, .login-gate-overlay, #appSplashScreen) 具备 overflow-y: auto 保护，杜绝低分辨率上下截断
6. 任务监控实时终端 (#termLogsContainer) 采用 clamp(220px, 32vh, 360px) 动态高度
7. 移动端媒体查询全面采用 100dvh 视口高度
"""

import os
import re
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_FILE = os.path.join(ROOT_DIR, "scripts", "approval_web.py")


class TestResponsiveViewport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WEB_FILE, "r", encoding="utf-8") as f:
            cls.html_content = f.read()

    def test_playground_arena_no_hardcoded_820px(self):
        """1. 演练场绝无 hardcoded 820px 固定死高度"""
        self.assertNotIn("height: 820px;", self.html_content, "不应再出现死高度 820px")
        # 确保使用 calc(100vh - 108px)
        self.assertIn("height: calc(100vh - 108px);", self.html_content)
        self.assertIn("min-height: 440px;", self.html_content)

    def test_chat_flow_flexbox_safety(self):
        """2. 演练场会话流容器具备 min-height: 0 规范"""
        # Flex 容器内部子元素若有内部滚动，必须具备 min-height: 0 避免撑大父容器
        m = re.search(r'\.chat-flow-container\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m, "必须找到 .chat-flow-container CSS 定义")
        css_body = m.group(1)
        self.assertIn("min-height: 0", css_body, ".chat-flow-container 必须声明 min-height: 0")
        self.assertIn("overflow-y: auto", css_body, ".chat-flow-container 必须声明 overflow-y: auto")

    def test_chat_input_docked_at_bottom(self):
        """3. 输入框包装器 flex-shrink: 0 稳固驻留底部"""
        m = re.search(r'\.chat-input-wrapper\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m, "必须找到 .chat-input-wrapper CSS 定义")
        css_body = m.group(1)
        self.assertIn("flex-shrink: 0", css_body, ".chat-input-wrapper 必须禁止收缩以稳定贴底")

    def test_mobile_arena_uses_dvh(self):
        """4. 移动端媒体查询采用 100dvh 动态视口高度"""
        self.assertIn("100dvh", self.html_content, "移动端样式必须支持 100dvh 动态视口高度适配")
        self.assertIn("calc(100dvh - 210px)", self.html_content)

    def test_all_modals_have_max_height_and_scroll(self):
        """5. 弹窗和蒙版具备视口高度保护与滚动保护"""
        # .modal-card
        m_modal = re.search(r'\.modal-card\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m_modal)
        self.assertIn("max-height: calc(100vh - 40px)", m_modal.group(1))
        self.assertIn("overflow-y: auto", m_modal.group(1))

        # .setting-modal-card
        m_setting = re.search(r'\.setting-modal-card\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m_setting)
        self.assertIn("max-height: calc(100vh - 48px)", m_setting.group(1))
        self.assertIn("overflow-y: auto", m_setting.group(1))

        # .modal-overlay
        m_overlay = re.search(r'\.modal-overlay\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m_overlay)
        self.assertIn("overflow-y: auto", m_overlay.group(1))

    def test_login_gate_and_splash_responsive(self):
        """6. 登录网关卡片与启动遮罩具备视口适配"""
        m_gate = re.search(r'\.login-gate-card\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m_gate)
        self.assertIn("max-height: calc(100vh - 48px)", m_gate.group(1))
        self.assertIn("overflow-y: auto", m_gate.group(1))

        m_splash = re.search(r'\.splash-card\s*\{([^}]+)\}', self.html_content)
        self.assertIsNotNone(m_splash)
        self.assertIn("max-height: calc(100vh - 40px)", m_splash.group(1))

    def test_term_logs_container_clamp(self):
        """7. 监控控制台高度采用 clamp 动态自适应"""
        self.assertIn("height:clamp(220px, 32vh, 360px)", self.html_content)

    def test_profile_popover_max_height(self):
        """8. 用户画像下拉卡片具备 max-height 视口保护 (桌面与移动双端)"""
        matches = re.findall(r'\.profile-popover-card\s*\{([^}]+)\}', self.html_content)
        self.assertGreaterEqual(len(matches), 2, "必须包含桌面端与移动端媒体查询样式")
        # 验证至少存在 calc(100vh - 84px) 与 calc(100dvh - 72px)
        combined = " ".join(matches)
        self.assertIn("max-height: calc(100vh - 84px)", combined)
        self.assertIn("max-height: calc(100dvh - 72px)", combined)
        self.assertIn("overflow-y: auto", combined)


if __name__ == "__main__":
    unittest.main()
