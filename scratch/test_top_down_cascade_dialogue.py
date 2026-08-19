from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.dialogue_manager import DialogueManager
from src.slot_store import Slot

kb = KnowledgeBase()
builder = OutputBuilder(kb)

print("==========================================================================")
print(" 从顶至底 (Class -> Family -> Type -> Unit) 逐层提问与候选展示模拟")
print("==========================================================================")

# 步骤 1：仅确定任务为 pipeline_inspection，询问 1 级 (equipment_class)
state_step1 = {"task_type_key": "pipeline_inspection"}
class_candidates = builder.resolve_allowed_values({"allowed_values_ref": "robot_classes"}, "pipeline_inspection", state_step1)
print("\n【步骤 1】询问机器人类别 (equipment_class):")
print(f"-> 呈现给用户的真正合法候选项: {class_candidates}")


# 步骤 2：用户回答了 equipment_class = "observation_rov"，询问 2 级 (equipment_family)
state_step2 = {"task_type_key": "pipeline_inspection", "equipment_class": "observation_rov"}
family_candidates = builder.resolve_allowed_values({"allowed_values_ref": "robot_family_full_names"}, "pipeline_inspection", state_step2)
print("\n【步骤 2】用户回答了 Class='观察级ROV'，询问作业机器人系列 (equipment_family):")
print(f"-> 呈现给用户的真正合法候选项: {family_candidates}")


# 步骤 3：用户回答了 equipment_family = "轻型工作级深海机器人"，询问 3 级 (equipment_type)
state_step3 = {"task_type_key": "pipeline_inspection", "equipment_class": "observation_rov", "equipment_family": "轻型工作级深海机器人"}
type_candidates = builder.resolve_allowed_values({"allowed_values_ref": "robot_variant_full_names"}, "pipeline_inspection", state_step3)
print("\n【步骤 3】用户回答了 Family='轻型工作级深海机器人'，询问设备型号 (equipment_type):")
print(f"-> 呈现给用户的真正合法候选项: {type_candidates}")


# 步骤 4：用户回答了 equipment_type = "轻型工作级深海机器人 150HP"，询问 4 级 (equipment_unit_id)
state_step4 = {"task_type_key": "pipeline_inspection", "equipment_class": "observation_rov", "equipment_family": "轻型工作级深海机器人", "equipment_type": "轻型工作级深海机器人 150HP"}
unit_candidates = builder._get_robot_unit_ids("pipeline_inspection", state_step4)
print("\n【步骤 4】用户回答了 Type='轻型工作级深海机器人 150HP'，询问单机编号 (equipment_unit_id):")
print(f"-> 呈现给用户的真正合法候选项: {unit_candidates}")
