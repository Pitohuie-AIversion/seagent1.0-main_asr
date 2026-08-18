"""
tests/test_multi_turn_soft_warning_persistence.py

回归测试：验证用户在多轮任务收集交互中“忽略软警告”后的持久有效性。
场景：
1. 第一轮：输入任务信息触发软警告（如光纤通信缆导致 C010 定位风险软警告），Phase -> blocked_soft；
2. 第二轮：用户输入“忽略警告”，Phase 放行恢复收集 -> collecting，软警告被写入 SlotStore.validation_acknowledgements；
3. 第三轮：用户补充下一个未填槽位（如“水深 350 米”），task_version 自增从 1 到 2，校验结果重新刷新；
4. 验证：
   - 之前已确认的软警告不会在第三轮再次触发 blocked_soft！
   - 之前忽略的软警告被判定为有效继承，UI 状态 constraint_state.ignored_soft_warnings 中包含该确认，soft_warnings 为空！
   - 流程正常推进，不重复弹窗骚扰用户。
"""

import unittest
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.slot_store import SlotStore, ValidationAcknowledgement
from src.validator import ValidationResult, Violation
from src.ui_state_builder import build_frontend_ui_state


class TestMultiTurnSoftWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.mock_llm = MagicMock()
        self.mock_kb = MagicMock()
        # Mock 基础 schema 与配置
        self.mock_kb.get_all_task_type_values.return_value = ["pipeline_inspection"]
        
    def test_soft_warning_ack_persists_across_slot_updates(self):
        """测试忽略软警告后，在后续槽位更新(task_version增加)时，软警告保持已忽略状态不重新弹窗。"""
        dm = DialogueManager(self.mock_llm, self.mock_kb)
        
        # 构造初始触发软警告的状态 (task_version = 1)
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "pipeline_type": "optical_fiber",
        }
        dm.phase = "blocked_soft"
        soft_v = Violation(
            constraint_id="C010",
            constraint_name="定位风险",
            message="DVL底锁失效风险",
            severity="soft",
            related_fields=["pipeline_type"],
        )
        dm._blocking_violations = [soft_v]
        
        # Step 1: 用户发送 “忽略警告”
        val_res = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-18T10:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_1",
            state_snapshot={"status_ref": "", "state_version": 0},
            violations=[soft_v],
        )
        def mock_refresh_1(purpose="interactive", changed_fields=None):
            dm.slot_store.validation_result = val_res
            return val_res
            
        with patch.object(dm, "_refresh_validation", side_effect=mock_refresh_1):
            reply1 = dm.process("忽略警告")
        
        # 验证忽略成功，进入 collecting，记录了 validation_acknowledgements
        self.assertIn("已记录您对当前软警告的确认", reply1)
        self.assertNotEqual(dm.phase, "blocked_soft")
        self.assertEqual(len(dm.slot_store.validation_acknowledgements), 1)
        ack = dm.slot_store.validation_acknowledgements[0]
        self.assertEqual(ack.constraint_id, "C010")
        
        # 记录此时的 task_version (设为 1)
        initial_version = dm.slot_store.version
        
        # Step 2: 模拟下一轮，用户补充下一个槽位 "水深 350 米"，导致 task_version 自增到 initial_version + 1
        with patch.object(dm.intent_router, "route") as mock_route:
            mock_route_res = MagicMock()
            mock_route_res.dialogue_mode = "task_collection"
            mock_route_res.interaction_type = "UPDATE"
            mock_route_res.interaction_plan = None
            mock_route.return_value = mock_route_res
            
            val_res2 = ValidationResult(
                overall_status="blocked_soft",
                validated_at="2026-08-18T10:00:00Z",
                task_version=2,
                validation_version=2,
                validation_fingerprint="fp_2",
                state_snapshot={"status_ref": "", "state_version": 0},
                violations=[soft_v],
            )
            def mock_refresh_2(purpose="interactive", changed_fields=None):
                dm.slot_store.validation_result = val_res2
                return val_res2
                
            def extract_with_version_increment(*args, **kwargs):
                dm.slot_store.version = initial_version + 1
                return {
                    "updates": {"water_depth": 350},
                    "unresolved": [],
                }
            with patch.object(dm.extractor, "extract_updates", side_effect=extract_with_version_increment):
                with patch.object(dm, "_refresh_validation", side_effect=mock_refresh_2):
                    reply2 = dm.process("水深350米")
                
        # 验证 task_version 确实增长了
        self.assertGreater(dm.slot_store.version, initial_version)
        
        # 验证：Phase 依然保持推进（没有被再次切回 blocked_soft！），回复中不包含“再次提示软警告”
        self.assertNotEqual(dm.phase, "blocked_soft")
        self.assertNotIn("当前仍存在软警告", reply2)
        
        # 验证 UI 状态：soft_warnings 列表为空，ignored_soft_warnings 中精确保留了 C010
        ui_state = build_frontend_ui_state(dm)
        cs = ui_state.get("constraint_state", {})
        self.assertEqual(len(cs.get("soft_warnings", [])), 0)
        self.assertEqual(len(cs.get("ignored_soft_warnings", [])), 1)
        self.assertEqual(cs["ignored_soft_warnings"][0]["constraint_id"], "C010")

    def test_external_state_refresh_auto_unblocks_constraints(self):
        """测试在 blocked_hard 或 blocked_soft 状态下，若管理员后台刷新了状态消除了违规，下一轮消息立刻自动解封并清空前后端警告。"""
        dm = DialogueManager(self.mock_llm, self.mock_kb)
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "pipeline_type": "optical_fiber",
        }
        dm.phase = "blocked_hard"
        hard_v = Violation(
            constraint_id="C999",
            constraint_name="通信中断",
            message="设备掉线",
            severity="hard",
            related_fields=[],
        )
        dm._blocking_violations = [hard_v]
        dm.slot_store.validation_result = ValidationResult(
            overall_status="blocked_hard",
            validated_at="2026-08-18T09:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_old",
            state_snapshot={"status_ref": "ref_offline", "state_version": 1},
            violations=[hard_v],
        )

        # 模拟后台管理员更新了遥测/状态文件，重新校验时违规已全部消除 (violations = [])
        clean_res = ValidationResult(
            overall_status="none",
            validated_at="2026-08-18T10:00:00Z",
            task_version=1,
            validation_version=2,
            validation_fingerprint="fp_clean",
            state_snapshot={"status_ref": "ref_online", "state_version": 2},
            violations=[],
        )
        def mock_clean_refresh(purpose="interactive", changed_fields=None):
            dm.slot_store.validation_result = clean_res
            return clean_res

        with patch.object(dm.intent_router, "route") as mock_route:
            mock_route_res = MagicMock()
            mock_route_res.dialogue_mode = "task_collection"
            mock_route_res.interaction_type = "QUERY"
            mock_route_res.interaction_plan = None
            mock_route.return_value = mock_route_res

            with patch.object(dm, "_refresh_validation", side_effect=mock_clean_refresh):
                reply = dm.process("确认")

        # 1. 验证 Phase 已经从 blocked_hard 自动跟随解封恢复为 collecting 或 confirming
        self.assertNotEqual(dm.phase, "blocked_hard")
        self.assertIn(dm.phase, ("collecting", "confirming"))
        self.assertEqual(len(dm._blocking_violations), 0)

        # 2. 验证 reply 中没有拒绝对话
        self.assertNotIn("重新校验发现硬约束", reply)

        # 3. 验证 ui_state 中 hard_violations 与 soft_warnings 均为空列表，右侧边栏卡片清空
        ui_state = build_frontend_ui_state(dm)
        cs = ui_state.get("constraint_state", {})
        self.assertEqual(len(cs.get("hard_violations", [])), 0)
        self.assertEqual(len(cs.get("soft_warnings", [])), 0)
        self.assertEqual(cs.get("status"), "none")


if __name__ == "__main__":
    unittest.main()
