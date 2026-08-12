"""test_model_profiles.py — ModelProfile 单元与契约测试

测试范围：
1. ModelProfile Schema 校验（合法配置、未知版本、缺失字段、非法角色、NaN/Inf/bool temp、<=0/bool max_tokens、非法 response_mode、非法 stop、重复角色映射、结构化角色保护）；
2. Legacy 模式 (model_profiles_v2 = false) 行为锁定（保持 enable_thinking=False 与 legacy 参数）；
3. Profile 模式 (model_profiles_v2 = true) 行为解析（各 ModelRole 映射匹配，GENERAL_REASONING 可开启 thinking）；
4. Fail-Closed 机制（配置缺失/非法/解析错误/结构化角色配置不当均安全阻断）；
5. Feature Flag 切换兼容；
6. LLMClient 公共 API 向后兼容性。
"""

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml

from src.llm_client import LLMClient
from src.model_profile import (
    GenerationOptions,
    ModelProfile,
    ModelProfileConfigError,
    ModelProfileError,
    ModelProfileNotFoundError,
    ModelProfileRegistry,
    ModelRole,
    is_model_profiles_v2_enabled,
    load_model_profiles,
    validate_profile,
)


class TestModelProfileValidation(unittest.TestCase):
    """测试 ModelProfile 配置与 Schema 校验逻辑。"""

    def setUp(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def tearDown(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def test_load_valid_model_profiles(self):
        """验证加载默认 config/model_profiles.yaml 配置正确无误。"""
        profiles = load_model_profiles()
        self.assertEqual(len(profiles), 7)
        self.assertIn(ModelRole.ROUTER, profiles)
        self.assertIn(ModelRole.EXTRACTOR, profiles)
        self.assertIn(ModelRole.TASK_RESPONDER, profiles)
        self.assertIn(ModelRole.KNOWLEDGE_QA, profiles)
        self.assertIn(ModelRole.GENERAL_REASONING, profiles)
        self.assertIn(ModelRole.FILTER_REPLY, profiles)
        self.assertIn(ModelRole.TRANSLATION, profiles)

        router_p = profiles[ModelRole.ROUTER]
        self.assertFalse(router_p.enable_thinking)
        self.assertEqual(router_p.response_mode, "json")

        gen_p = profiles[ModelRole.GENERAL_REASONING]
        self.assertTrue(gen_p.enable_thinking)
        self.assertEqual(gen_p.response_mode, "text")

    def test_reject_unknown_schema_version(self):
        """验证拒绝不支持的 schema_version。"""
        data = {"schema_version": 2, "profiles": {}}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.dump(data, f)
            f_path = Path(f.name)
        try:
            with self.assertRaises(ModelProfileConfigError):
                load_model_profiles(f_path)
        finally:
            f_path.unlink()

    def test_reject_missing_profile_field(self):
        """验证拒绝缺失必需字段的 Profile。"""
        bad_profile = {
            "name": "bad",
            # missing role & enable_thinking
            "temperature": 0.1,
            "max_tokens": 100,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(bad_profile, "bad")

    def test_reject_invalid_role(self):
        """验证拒绝未知的 ModelRole。"""
        data = {
            "role": "invalid_role_name",
            "enable_thinking": False,
            "temperature": 0.1,
            "max_tokens": 100,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_nan_temperature(self):
        """验证拒绝 NaN 温度配置。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": float("nan"),
            "max_tokens": 100,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_inf_temperature(self):
        """验证拒绝 Inf 温度配置。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": float("inf"),
            "max_tokens": 100,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_bool_temperature(self):
        """验证拒绝 bool 类型的 temperature（Python 中 bool 为 int 子类）。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": True,
            "max_tokens": 100,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_zero_max_tokens(self):
        """验证拒绝 max_tokens <= 0。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": 0.7,
            "max_tokens": 0,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_negative_max_tokens(self):
        """验证拒绝负数 max_tokens。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": 0.7,
            "max_tokens": -50,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_bool_max_tokens(self):
        """验证拒绝 bool 类型的 max_tokens。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": 0.7,
            "max_tokens": True,
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_invalid_response_mode(self):
        """验证拒绝 'text' 与 'json' 之外的 response_mode。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": 0.7,
            "max_tokens": 500,
            "response_mode": "xml",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_invalid_stop(self):
        """验证拒绝包含非 str 元素的 stop 列表。"""
        data = {
            "role": "general_reasoning",
            "enable_thinking": True,
            "temperature": 0.7,
            "max_tokens": 500,
            "stop": [123, "stop_seq"],
            "response_mode": "text",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(data, "test")

    def test_reject_duplicate_role_or_profile(self):
        """验证拒绝映射至同一个角色的重复 Profile。"""
        yaml_content = {
            "schema_version": 1,
            "profiles": {
                "p1": {
                    "role": "router",
                    "enable_thinking": False,
                    "temperature": 0.1,
                    "max_tokens": 260,
                    "response_mode": "json",
                },
                "p2": {
                    "role": "router",
                    "enable_thinking": False,
                    "temperature": 0.2,
                    "max_tokens": 300,
                    "response_mode": "json",
                },
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.dump(yaml_content, f)
            f_path = Path(f.name)
        try:
            with self.assertRaises(ModelProfileConfigError):
                load_model_profiles(f_path)
        finally:
            f_path.unlink()

    def test_structured_role_protection(self):
        """验证结构化协议角色 (ROUTER/EXTRACTOR) 被配置 enable_thinking=True 或 response_mode=text 时触发拒绝。"""
        bad_router_thinking = {
            "role": "router",
            "enable_thinking": True,  # Violation!
            "temperature": 0.1,
            "max_tokens": 260,
            "response_mode": "json",
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(bad_router_thinking, "bad_router")

        bad_extractor_text = {
            "role": "extractor",
            "enable_thinking": False,
            "temperature": 0.1,
            "max_tokens": 800,
            "response_mode": "text",  # Violation!
        }
        with self.assertRaises(ModelProfileConfigError):
            validate_profile(bad_extractor_text, "bad_extractor")


class TestLegacyModeBehaviors(unittest.TestCase):
    """测试 Feature Flag model_profiles_v2 = false 时的 Legacy 契约与回滚基线。"""

    def setUp(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def tearDown(self):
        ModelProfileRegistry.get_instance().clear_cache()

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=False)
    @patch("src.llm_client.SamplingParams", MagicMock())
    def test_legacy_generate_text_keeps_thinking_false(self, mock_v2):
        """验证 Legacy 模式下，apply_chat_template 始终强制 enable_thinking=False。"""
        mock_tok = MagicMock()
        mock_tok.apply_chat_template.return_value = "prompt"
        mock_llm = MagicMock()
        mock_output = MagicMock()
        mock_output.outputs = [MagicMock(text="response")]
        mock_llm.generate.return_value = [mock_output]

        client = LLMClient(mock_llm, mock_tok)
        client.generate_text([{"role": "user", "content": "hi"}], temperature=0.7, max_tokens=1500)

        mock_tok.apply_chat_template.assert_called_once()
        _, kwargs = mock_tok.apply_chat_template.call_args
        self.assertIn("enable_thinking", kwargs)
        self.assertFalse(kwargs["enable_thinking"])

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=False)
    def test_legacy_router_generation_contract(self, mock_v2):
        """Legacy 配置也不得恢复离线关键词路由器。"""
        client = LLMClient(None, None)
        res = client.classify_interaction([{"role": "user", "content": "巡检任务"}])
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("operation"), "CLARIFY")
        self.assertEqual(res.get("dialogue_mode"), "knowledge_qa")
        self.assertTrue(res.get("needs_clarification"))
        self.assertEqual(
            res.get("reason_code"),
            "OFFLINE_SEMANTIC_MODEL_UNAVAILABLE",
        )

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=False)
    def test_legacy_extractor_generation_contract(self, mock_v2):
        """验证 Legacy 模式下，extract_slots 仍然保持原始离线 mock 契约。"""
        client = LLMClient(None, None)
        res = client.extract_slots([{"role": "user", "content": "水深300米"}])
        self.assertIsInstance(res, dict)
        self.assertIn("slot_candidates", res)


class TestProfileModeBehaviors(unittest.TestCase):
    """测试 Feature Flag model_profiles_v2 = true 时的 Profile 解析与驱动能力。"""

    def setUp(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def tearDown(self):
        ModelProfileRegistry.get_instance().clear_cache()

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_router_profile_applied(self, mock_v2):
        """验证 ModelRole.ROUTER 正确解析为路由器 Profile 且 enable_thinking=False, response_mode=json。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.ROUTER,
            default_temp=0.1,
            default_max_tokens=260,
            default_response_mode="json",
        )
        self.assertEqual(options.role, ModelRole.ROUTER)
        self.assertFalse(options.enable_thinking)
        self.assertEqual(options.response_mode, "json")
        self.assertEqual(options.temperature, 0.1)
        self.assertEqual(options.max_tokens, 260)

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_extractor_profile_applied(self, mock_v2):
        """验证 ModelRole.EXTRACTOR 正确解析为抽取器 Profile 且 enable_thinking=False, response_mode=json。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.EXTRACTOR,
            default_temp=0.1,
            default_max_tokens=800,
            default_response_mode="json",
        )
        self.assertEqual(options.role, ModelRole.EXTRACTOR)
        self.assertFalse(options.enable_thinking)
        self.assertEqual(options.response_mode, "json")

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_general_reasoning_profile_can_enable_thinking(self, mock_v2):
        """验证 ModelRole.GENERAL_REASONING 能独立开启 enable_thinking=True。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.GENERAL_REASONING,
            default_temp=0.7,
            default_max_tokens=1500,
            default_response_mode="text",
        )
        self.assertEqual(options.role, ModelRole.GENERAL_REASONING)
        self.assertTrue(options.enable_thinking)
        self.assertEqual(options.response_mode, "text")

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_filter_reply_profile_applied(self, mock_v2):
        """验证 ModelRole.FILTER_REPLY 正确解析 Profile。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.FILTER_REPLY,
            default_temp=0.7,
            default_max_tokens=1500,
            default_response_mode="text",
        )
        self.assertEqual(options.role, ModelRole.FILTER_REPLY)
        self.assertFalse(options.enable_thinking)

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_translation_profile_applied(self, mock_v2):
        """验证 ModelRole.TRANSLATION 正确解析 Profile。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.TRANSLATION,
            default_temp=0.1,
            default_max_tokens=1500,
            default_response_mode="text",
        )
        self.assertEqual(options.role, ModelRole.TRANSLATION)
        self.assertFalse(options.enable_thinking)
        self.assertEqual(options.temperature, 0.1)


class TestFailClosedBehaviors(unittest.TestCase):
    """测试 model_profiles_v2 = true 时的 Fail-Closed 阻断机制。"""

    def setUp(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def tearDown(self):
        ModelProfileRegistry.get_instance().clear_cache()

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_missing_profile_raises(self, mock_v2):
        """验证在 V2 模式下对未定义/缺失角色的 Profile 查询直接抛出 ModelProfileNotFoundError。"""
        client = LLMClient(None, None)
        with self.assertRaises(ModelProfileNotFoundError):
            client._resolve_generation_options(
                role="non_existent_role",
                default_temp=0.7,
                default_max_tokens=1500,
            )

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_invalid_profile_file_raises(self, mock_v2):
        """验证配置文件解析失败时触发 Fail-Closed 抛出异常。"""
        with patch.object(Path, "exists", return_value=False):
            client = LLMClient(None, None)
            with self.assertRaises(ModelProfileNotFoundError):
                client._resolve_generation_options(
                    role=ModelRole.ROUTER,
                    default_temp=0.1,
                    default_max_tokens=260,
                )


class TestFeatureFlagSwitching(unittest.TestCase):
    """测试 Feature Flag 开启与关闭时的安全行为对比。"""

    def setUp(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def tearDown(self):
        ModelProfileRegistry.get_instance().clear_cache()

    def test_feature_flag_default_is_false(self):
        """验证 config/features.yaml 默认配置中 model_profiles_v2 为 False。"""
        self.assertFalse(is_model_profiles_v2_enabled())

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=False)
    def test_flag_false_returns_legacy_options(self, mock_v2):
        """验证 flag=false 时即使指定角色仍退回 Legacy 选项机制。"""
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.GENERAL_REASONING,
            default_temp=0.7,
            default_max_tokens=1500,
        )
        self.assertFalse(options.enable_thinking)
        self.assertEqual(options.profile_name, "legacy")


class TestPublicAPICompatibility(unittest.TestCase):
    """测试 LLMClient 所有公共 API 在未显式传 role 参数时的向后兼容性。"""

    def setUp(self):
        self.client = LLMClient(None, None)

    def test_all_public_methods_callable_without_role(self):
        """验证所有旧公共 API 方法可不带 role 参数正常被调用。"""
        messages = [{"role": "user", "content": "hello"}]

        text_res = self.client.generate_text(messages)
        self.assertIsInstance(text_res, str)

        json_res = self.client.generate_json(messages)
        self.assertTrue(json_res is None or isinstance(json_res, (dict, list)))

        classify_res = self.client.classify_interaction(messages)
        self.assertTrue(classify_res is None or isinstance(classify_res, dict))

        slots_res = self.client.extract_slots(messages)
        self.assertIsInstance(slots_res, dict)

        gen_res = self.client.generate(messages)
        self.assertIsInstance(gen_res, str)

        chat_res = self.client.chat(messages)
        self.assertIsInstance(chat_res, str)

        ej_res = self.client.extract_json(messages)
        self.assertIsInstance(ej_res, dict)

        filter_res = self.client.filter_reply("测试脱敏文本")
        self.assertIsInstance(filter_res, str)


class TestFeatureFlagStrictType(unittest.TestCase):
    """测试 model_profiles_v2 Feature Flag 严格类型校验与解析异常捕获。"""

    def _write_temp_features(self, yaml_obj: object) -> Path:
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump(yaml_obj, f)
        f.close()
        return Path(f.name)

    def test_feature_flag_false_boolean_is_false(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": False}})
        try:
            self.assertFalse(is_model_profiles_v2_enabled(p))
        finally:
            p.unlink()

    def test_feature_flag_true_boolean_is_true(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": True}})
        try:
            self.assertTrue(is_model_profiles_v2_enabled(p))
        finally:
            p.unlink()

    def test_feature_flag_missing_key_defaults_false(self):
        p = self._write_temp_features({"features": {}})
        try:
            self.assertFalse(is_model_profiles_v2_enabled(p))
        finally:
            p.unlink()

    def test_feature_flag_rejects_string_false(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": "false"}})
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()

    def test_feature_flag_rejects_string_true(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": "true"}})
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()

    def test_feature_flag_rejects_integer_zero(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": 0}})
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()

    def test_feature_flag_rejects_integer_one(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": 1}})
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()

    def test_feature_flag_rejects_null(self):
        p = self._write_temp_features({"features": {"model_profiles_v2": None}})
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()

    def test_feature_flag_yaml_parse_failure_is_explicit(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        f.write("invalid: yaml: [:\n")
        f.close()
        p = Path(f.name)
        try:
            with self.assertRaises(ModelProfileConfigError):
                is_model_profiles_v2_enabled(p)
        finally:
            p.unlink()


class TestRoleTypeErrorFallback(unittest.TestCase):
    """测试 TypeError role 兼容降级与内部 TypeError 不被吞掉的防护。"""

    def test_role_compatibility_falls_back_for_old_signature(self):
        class OldLLM:
            def chat(self, messages, temperature=0.7, max_tokens=1500):
                return "old_chat_response"

        from src.dialogue_manager import DialogueManager
        dm = DialogueManager()
        dm.llm = OldLLM()
        res = dm._safe_llm_chat([{"role": "user", "content": "hi"}], role=ModelRole.GENERAL_REASONING)
        self.assertEqual(res, "old_chat_response")

    def test_role_compatibility_does_not_swallow_internal_type_error(self):
        class BrokenLLM:
            def chat(self, messages, temperature=0.7, max_tokens=1500, role=None):
                raise TypeError("Internal conversion failure: int object is not callable")

        from src.dialogue_manager import DialogueManager
        dm = DialogueManager()
        dm.llm = BrokenLLM()

        with self.assertRaises(TypeError) as ctx:
            dm._safe_llm_chat([{"role": "user", "content": "hi"}], role=ModelRole.TASK_RESPONDER)
        self.assertIn("Internal conversion failure", str(ctx.exception))


class TestTranslationWiring(unittest.TestCase):
    """测试真实 Translation 入口角色绑定与 Profile 驱动。"""

    @patch("web_backend._shared_llm")
    def test_translation_call_uses_translation_profile_when_v2_enabled(self, mock_llm):
        mock_llm.chat.return_value = "Hello"
        from web_backend import _translate_single_chunk
        _translate_single_chunk("你好", "English")

        mock_llm.chat.assert_called_once()
        _, kwargs = mock_llm.chat.call_args
        self.assertEqual(kwargs.get("role"), ModelRole.TRANSLATION)

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=False)
    @patch("src.llm_client.SamplingParams", MagicMock())
    def test_translation_legacy_parameters_unchanged_when_flag_false(self, mock_v2):
        mock_tok = MagicMock()
        mock_tok.apply_chat_template.return_value = "prompt"
        mock_llm = MagicMock()
        mock_output = MagicMock()
        mock_output.outputs = [MagicMock(text="Hello")]
        mock_llm.generate.return_value = [mock_output]

        client = LLMClient(mock_llm, mock_tok)
        res = client.chat([{"role": "user", "content": "你好"}], temperature=0.1, max_tokens=1500, role=ModelRole.TRANSLATION)
        self.assertEqual(res, "Hello")

        mock_tok.apply_chat_template.assert_called_once()
        _, kwargs = mock_tok.apply_chat_template.call_args
        self.assertFalse(kwargs["enable_thinking"])

    @patch("src.llm_client.is_model_profiles_v2_enabled", return_value=True)
    def test_translation_v2_uses_profile_options(self, mock_v2):
        client = LLMClient(None, None)
        options = client._resolve_generation_options(
            role=ModelRole.TRANSLATION,
            default_temp=0.1,
            default_max_tokens=1500,
        )
        self.assertEqual(options.role, ModelRole.TRANSLATION)
        self.assertFalse(options.enable_thinking)
        self.assertEqual(options.temperature, 0.1)


class TestKnowledgeQAContextResolution(unittest.TestCase):
    """测试 Knowledge QA 恢复 equipment_type context 与 follow-up query 上下文解析。"""

    def test_device_query_preserves_selected_equipment_context(self):
        from src.dialogue_manager import DialogueManager
        from src.intent_router import IntentRouteResult

        dm = DialogueManager()
        # 设置真实任务上下文：已有任务类型与设备名称
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.task_state["equipment_name"] = "观察级ROV"

        # 记录执行前 SlotStore 版本号
        version_before = dm.slot_store.version

        route = IntentRouteResult(
            dialogue_mode="knowledge_qa",
            interaction_type="QUERY",
            query_intent="DEVICE_CAPABILITY",
            confidence=0.95,
            reason="测试 follow-up 设备能力询问",
        )

        user_msg = "它最大能下潜多少米？"  # 消息中不包含设备名称实体
        reply = dm._handle_knowledge_query(user_msg, route, request_id="req_followup_test")

        # 断言没返回 device_not_resolved 错误
        self.assertNotIn("未找到该设备信息", reply)
        self.assertNotIn("请说明具体的机器人型号或名称", reply)

        # 断言使用了当前任务选中的设备信息
        self.assertTrue("观察级" in reply or "符合条件" in reply or "600" in reply or "米" in reply)

        # 断言 QUERY path 不修改 SlotStore (INV-01)
        self.assertEqual(dm.slot_store.version, version_before)


if __name__ == "__main__":
    unittest.main()
