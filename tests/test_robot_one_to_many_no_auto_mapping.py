"""
tests/test_robot_one_to_many_no_auto_mapping.py

验证“机器人父子级关系存在一对多时不得自动映射写入”核心约束：
1. 父子级一对多时，系统绝不自动猜测或给子级槽位写入任何值；
2. 只有从子级向上反推祖先（1对1关系）时允许自动补全祖先；
3. 当用户仅指定上级（如设备型号或系列），而对应下级包含多台单机（如 LROV-150-001, LROV-150-002）时，
   系统必须在交互收集阶段停下来引导用户选择，不得自动给机器写入任何单机编号，也不得误报 VAL_ERR 硬阻断；
4. 快照重构逻辑 (SlotStore) 绝不跨越最深显示选择器去下查或发明后代槽位值；
5. 在模型选型推荐或用户接受推荐场景下，系统仅作用于设备型号 (equipment_type)，绝对不自动将具体单机编号写入 equipment_unit_id。
"""

import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.interaction_plan import validate_interaction_plan
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from src.validator import TaskValidator
from tests.interaction_plan_support import make_plan


class TestRobotOneToManyNoAutoMapping(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.validator = TaskValidator(self.kb)

    def test_class_to_family_one_to_many_does_not_auto_map_family(self):
        """当 equipment_class 下有多个 family 时，绝不自动写入 equipment_family。"""
        dm = DialogueManager(llm=MagicMock(), kb=self.kb)

        # 假设任务只设置了 equipment_class="observation_rov"
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_class": "observation_rov",
        }
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.slot_store.slots["equipment_class"] = Slot("equipment_class", value="observation_rov", status="valid")

        # 运行级联重算
        dm._auto_collapse_robot_cascade(dm.slot_store.slots)

        # 验证：equipment_family 槽位绝对不能被自动写入（因为观察级ROV下有多个 family 候选）
        fam_slot = dm.slot_store.slots.get("equipment_family")
        self.assertTrue(
            fam_slot is None or fam_slot.status == "missing" or fam_slot.value is None,
            "When class-to-family is one-to-many, equipment_family must NOT be auto-mapped or written.",
        )

    def test_type_to_unit_one_to_many_does_not_auto_map_unit(self):
        """当设备型号 (equipment_type) 包含多台单机 (LROV-150-001, LROV-150-002) 时，绝不自动写入设备单机编号。"""
        dm = DialogueManager(llm=MagicMock(), kb=self.kb)

        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_type": "轻型工作级深海机器人 150HP",
        }
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.slot_store.slots["equipment_type"] = Slot("equipment_type", value="轻型工作级深海机器人 150HP", status="valid")

        dm._auto_collapse_robot_cascade(dm.slot_store.slots)

        # 向上祖先（equipment_family, equipment_class）可被确定性补全（1对1）
        self.assertEqual(dm.slot_store.slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(dm.slot_store.slots["equipment_class"].value, "observation_rov")

        # 但向下子级（equipment_unit_id）存在多台单机（一对多），绝不能自动填入其中任何一台
        unit_slot = dm.slot_store.slots.get("equipment_unit_id")
        self.assertTrue(
            unit_slot is None or unit_slot.status == "missing" or unit_slot.value is None,
            "When type-to-unit is one-to-many, equipment_unit_id must NOT be auto-mapped.",
        )

    def test_interactive_validation_allows_unselected_multi_unit_without_hard_blocking(self):
        """在交互收集模式下，选定有多个单机候选的型号时，验证器不得报 VAL_ERR 阻断，也不得给机器写入默认单机。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "start_time": "2026-08-20T10:00:00+08:00",
            "end_time": "2026-08-20T12:00:00+08:00",
            "water_depth": 100.0,
            "equipment_class": "observation_rov",
            "equipment_family": "轻型工作级深海机器人",
            "equipment_type": "轻型工作级深海机器人 150HP",
            # 注意：故意不填 equipment_unit_id
        }

        # 交互收集模式校验
        val_res = self.validator.validate_task(task_state, purpose="interactive")

        # 不得由于多台单机存在而产生 hard 违规阻断
        hard_violations = [v for v in val_res.violations if v.severity == "hard"]
        self.assertEqual(
            len(hard_violations),
            0,
            f"Interactive validation must not hard-block when multiple fleet units exist: {hard_violations}",
        )
        self.assertIsNone(val_res.state_snapshot, "Telemetry snapshot must remain None until unit_id is specified.")

    def test_snapshot_lineage_reconstruction_does_not_invent_descendants(self):
        """验证 SlotStore 快照恢复逻辑仅反推最深显式选择器的祖先，绝对不往下猜测或发明后代。"""
        restored_task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_family": "轻型工作级深海机器人",
        }

        # 模拟只有 equipment_family 的快照
        canonical_selection = self.kb.validate_robot_selection_from_task_state(
            restored_task_state,
            require_unit=False,
        )

        # 验证返回的 canonical_selection 只包含祖先 robot_class，不包含后代 variant_id 或 unit_id
        self.assertIn("robot_class", canonical_selection)
        self.assertNotIn("variant_id", canonical_selection)
        self.assertNotIn("unit_id", canonical_selection)

    def test_grounded_recommendation_never_recommends_unit_id(self):
        """验证接地推荐机制绝不拦截或自动为 equipment_unit_id 生成推荐文本。"""
        dm = DialogueManager(llm=MagicMock(), kb=self.kb)

        # 构造针对设备的推荐路由结果
        raw_plan = make_plan(
            "READ",
            relation="recommend",
            subject_type="device",
            subject_text="LROV-150-001",
        )
        plan = validate_interaction_plan(raw_plan)
        route = plan.to_intent_route_result()

        # 当处于仅剩 equipment_unit_id 待填时
        dm._last_missing = [
            {
                "key": "equipment_unit_id",
                "label": "具体机器人编号",
                "allowed_values": ["LROV-150-001", "LROV-150-002"],
            }
        ]

        # 运行接地推荐
        rec_text = dm._build_grounded_recommendation(route, "推荐哪个单机好？")

        # 必须返回 None，不能直接绑定并给单机做推荐
        self.assertIsNone(rec_text, "Grounded recommendation must return None for equipment_unit_id.")

    def test_accept_recommendation_only_writes_type_not_unit_id(self):
        """验证用户接收设备推荐时，仅将设备型号写入槽位，绝对不连带写入单机编号。"""
        dm = DialogueManager(llm=MagicMock(), kb=self.kb)
        dm.conversation_history = [
            {
                "role": "assistant",
                "content": "针对当前管缆巡检任务，我明确推荐作业设备型号【轻型工作级深海机器人 150HP】。",
            }
        ]
        dm._last_missing = [
            {
                "key": "equipment_type",
                "label": "作业设备型号",
                "allowed_values": ["轻型工作级深海机器人 150HP"],
            }
        ]

        raw_plan = make_plan(
            "WRITE",
            relation="recommend",
            subject_type="device",
            subject_text="轻型工作级深海机器人 150HP",
        )
        plan = validate_interaction_plan(raw_plan)

        extraction_result = {
            "slot_candidates": [
                {
                    "raw_key": "型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "接受推荐",
                    "normalized_value": "轻型工作级深海机器人 150HP",
                }
            ],
            "unresolved": [],
        }

        scoped = dm._scope_confirmed_recommendation(
            extraction_result,
            plan,
            "好的，就用这个推荐",
        )

        # 验证 scoped_candidates 中只授权了 equipment_type，绝无 equipment_unit_id
        keys = [c.get("canonical_key") for c in scoped["slot_candidates"]]
        self.assertIn("equipment_type", keys)
        self.assertNotIn("equipment_unit_id", keys)
