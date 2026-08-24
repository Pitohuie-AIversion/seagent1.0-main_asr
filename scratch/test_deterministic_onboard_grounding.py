from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan, extraction_result, slot_candidate

kb = KnowledgeBase()
llm = ScriptedLLM(default_plan=make_plan("WRITE"))
dm = DialogueManager(llm, kb)

# Round 1: Select task type pipeline_inspection and robot 75HP
llm.queue_extraction(extraction_result(
    slot_candidate("task_type_key", "pipeline_inspection", raw_key="任务类型", raw_value="管缆巡检"),
    slot_candidate("equipment_type", "观察级深海机器人 75HP", raw_key="作业设备型号", raw_value="观察级深海机器人 75HP"),
))

res = dm.process("创建电力电缆巡检任务，选择观察级深海机器人75HP")
print("=== Grounded Reply ===")
print(res)

assert "观察级深海机器人 75HP" in res, f"Expected '观察级深海机器人 75HP' in reply but got: {res}"
assert "出厂已自带标配" in res, f"Expected '出厂已自带标配' in reply but got: {res}"
assert "高清水下摄像机" in res, f"Expected '高清水下摄像机' in reply but got: {res}"
assert "激光标尺" in res, f"Expected '激光标尺' in reply but got: {res}"

print("\n=== TEST PASSED PERFECTLY ===")
