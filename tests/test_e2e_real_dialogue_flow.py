# -*- coding: utf-8 -*-
"""
End-to-End Real Dialogue Flow Integration Tests

包含真实 DialogueManager 对话交互链路测试及 Flask Web Backend HTTP 端到端 API 测试，
验证按任务模板限定槽位、非模板槽位拒绝引导及正确模板槽位解析。
"""

import unittest
from unittest.mock import MagicMock
from src.dialogue_manager import DialogueManager
from src.slot_store import Slot
from src.intent_router import IntentRouteResult
from web_backend import app, init_manager, get_or_create_manager


class TestEndToEndDialogueFlow(unittest.TestCase):

    def setUp(self):
        self.dm = DialogueManager(llm=MagicMock())
        init_manager(self.dm)

    def test_e2e_pipeline_inspection_rejects_oilfield_with_guidance(self):
        """端到端测试：在管缆巡检任务中输入油田名称，系统必须拒绝油田写入并给出坐标引导信息。"""
        # 1. 模拟用户发起管缆巡检任务并填报油田
        self.dm.task_type_key = "pipeline_inspection"
        tt_slot = Slot("task_type", value_type="string")
        tt_slot.value = "管缆巡检"
        tt_slot.status = "valid"
        ttk_slot = Slot("task_type_key", value_type="string")
        ttk_slot.value = "pipeline_inspection"
        ttk_slot.status = "valid"
        self.dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot}, [])

        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "raw_value": "300米",
                    "confidence": 1.0,
                },
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "流花11-1油田",
                    "raw_value": "流花11-1油田",
                    "confidence": 1.0,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        reply = self.dm.process("创建水深300米的管缆巡检任务，位于流花11-1油田")

        # 校验：水深填报成功，油田槽位未被写入
        self.assertEqual(self.dm.task_state.get("water_depth"), 300)
        self.assertNotIn("oilfield_name", self.dm.task_state)

        # 校验：回复中包含针对非油田模板任务的专属坐标引导提示
        self.assertIn("当前任务类型‘管缆巡检’未包含油田槽位", reply)
        self.assertIn("无法通过油田名称“流花11-1油田”进行坐标映射", reply)

    def test_e2e_tree_valve_operation_accepts_oilfield(self):
        """端到端测试：在水面采油树阀门操作任务中输入油田名称，系统必须正常写入油田。"""
        self.dm.task_type_key = "tree_valve_operation"
        tt_slot = Slot("task_type", value_type="string")
        tt_slot.value = "水面采油树阀门操作"
        tt_slot.status = "valid"
        ttk_slot = Slot("task_type_key", value_type="string")
        ttk_slot.value = "tree_valve_operation"
        ttk_slot.status = "valid"
        self.dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot}, [])

        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "流花11-1油田",
                    "raw_value": "流花11-1油田",
                    "confidence": 1.0,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        reply = self.dm.process("在流花11-1油田进行阀门操作")

        # 校验：油田槽位成功写入
        self.assertEqual(self.dm.task_state.get("oilfield_name"), "流花11-1油田")
        self.assertNotIn("当前任务类型", reply)

    def test_flask_http_api_chat_pipeline_inspection_e2e(self):
        """端到端 HTTP API 测试：通过 Flask Client 发起 /api/chat 访问。"""
        client = app.test_client()
        session_id = "test_e2e_session"
        dm = get_or_create_manager(session_id)
        dm.task_type_key = "pipeline_inspection"
        tt_slot = Slot("task_type", value_type="string")
        tt_slot.value = "管缆巡检"
        tt_slot.status = "valid"
        ttk_slot = Slot("task_type_key", value_type="string")
        ttk_slot.value = "pipeline_inspection"
        ttk_slot.status = "valid"
        dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot}, [])

        dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "流花11-1油田",
                    "raw_value": "流花11-1油田",
                    "confidence": 1.0,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        response = client.post(
            "/api/chat",
            json={"message": "位于流花11-1油田", "session_id": session_id},
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertEqual(json_data.get("code"), 200)
        reply_text = json_data.get("reply", "")
        self.assertIn("当前任务类型‘管缆巡检’未包含油田槽位", reply_text)


if __name__ == "__main__":
    unittest.main()
