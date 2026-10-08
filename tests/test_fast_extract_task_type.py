import unittest
from unittest.mock import MagicMock, patch
from src.extraction.extractor import ParameterExtractor
from src.llm_client import LLMClient


class TestFastExtractTaskType(unittest.TestCase):
    """验证方案A性能优化：首轮任务类型快路径抽取与回退机制。"""

    def setUp(self):
        flag = patch("src.extraction.extractor.get_feature_flag", return_value=True)
        flag.start()
        self.addCleanup(flag.stop)
        self.mock_llm = MagicMock(spec=LLMClient)
        self.extractor = ParameterExtractor(self.mock_llm)
        self.task_type_map = {
            "管缆巡检": "pipeline_inspection",
            "管缆埋设": "pipeline_burial",
            "采油树控制面板插入": "tree_valve_operation",
            "采油树控制面板拔出": "tree_valve_operation",
        }

    def test_fast_extract_pipeline_inspection(self):
        """明确提及管缆巡检时，快路径命中且不调用 LLM。"""
        res = self.extractor.extract_updates(
            "安排一个管缆巡检任务，水深50米",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_not_called()
        self.assertIn("slot_candidates", res)
        cands = {c["canonical_key"]: c["normalized_value"] for c in res["slot_candidates"]}
        self.assertEqual(cands.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(cands.get("task_type"), "管缆巡检")

    def test_fast_extract_emergency_mode(self):
        """明确提及紧急时，同时提取 emergency_mode。"""
        res = self.extractor.extract_updates(
            "加急管缆埋设作业",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_not_called()
        cands = {c["canonical_key"]: c["normalized_value"] for c in res["slot_candidates"]}
        self.assertEqual(cands.get("task_type_key"), "pipeline_burial")
        self.assertEqual(cands.get("emergency_mode"), True)

    def test_conflicting_task_types_fallbacks_to_llm(self):
        """若同时出现多个冲突任务类型，快路径放弃并回退到 LLM。"""
        self.mock_llm.extract_json.return_value = {
            "slot_candidates": [],
            "unresolved": ["同轮具体任务类型互相冲突，请只指定一种任务操作。"],
        }
        res = self.extractor.extract_updates(
            "先做巡检再做埋设",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_called_once()
        self.assertIn("同轮具体任务类型互相冲突", "".join(res["unresolved"]))

    def test_unsupported_task_fallbacks_to_llm(self):
        """若包含不支持的打捞任务，快路径放弃并回退到 LLM。"""
        self.mock_llm.extract_json.return_value = {
            "slot_candidates": [],
            "unresolved": ["不支持打捞沉船任务。"],
        }
        res = self.extractor.extract_updates(
            "帮我打捞沉船",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_called_once()
        self.assertIn("不支持", "".join(res["unresolved"]))

    def test_vague_input_fallbacks_to_llm(self):
        """模糊输入无任务类型关键词时，回退到 LLM 正常处理。"""
        self.mock_llm.extract_json.return_value = {
            "slot_candidates": [],
            "unresolved": ["未识别到明确任务类型。"],
        }
        res = self.extractor.extract_updates(
            "今天天气怎么样",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_called_once()


    def test_tree_valve_operation_without_insert_withdraw_fallbacks_to_llm(self):
        """采油树阀门操作未指定插入或拔出时，属于操作歧义，快路径必须回退到 LLM。"""
        self.mock_llm.extract_json.return_value = {
            "slot_candidates": [],
            "unresolved": ["用户描述为'水面采油树阀门操作'，未明确是'插入'还是'拔出'，无法确定具体 task_type 值。"],
        }
        res = self.extractor.extract_updates(
            "在崖城13-1气田进行水面采油树阀门操作，水深50米",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_called_once()
        self.assertIn("未明确是'插入'还是'拔出'", "".join(res.get("unresolved", [])))

    def test_tree_valve_operation_with_explicit_insert_or_withdraw(self):
        """明确指定采油树插入或拔出时，快路径精确命中且不调用 LLM。"""
        res_insert = self.extractor.extract_updates(
            "执行采油树控制面板插入作业",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        self.mock_llm.extract_json.assert_not_called()
        cands_ins = {c["canonical_key"]: c["normalized_value"] for c in res_insert["slot_candidates"]}
        self.assertEqual(cands_ins.get("task_type"), "采油树控制面板插入")
        self.assertEqual(cands_ins.get("task_type_key"), "tree_valve_operation")

        res_withdraw = self.extractor.extract_updates(
            "安排水下采油树控制面板拔出任务",
            current_state={},
            task_type_key=None,
            task_type_map=self.task_type_map,
        )
        cands_wd = {c["canonical_key"]: c["normalized_value"] for c in res_withdraw["slot_candidates"]}
        self.assertEqual(cands_wd.get("task_type"), "采油树控制面板拔出")
        self.assertEqual(cands_wd.get("task_type_key"), "tree_valve_operation")


if __name__ == "__main__":
    unittest.main()
