import unittest
from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder

kb = KnowledgeBase()
builder = OutputBuilder(kb)

print("==========================================================================")
print(" 载荷推荐阶段微调动态机制校验报告")
print("==========================================================================")

# 场景 1：仅确定了任务类型 (管缆巡检 pipeline_inspection)，未确定机器人
task_state_stage1 = {
    "task_type_key": "pipeline_inspection",
    "equipment_type": None
}

field_def = {
    "key": "payloads",
    "label": "搭载工具/载荷",
    "type": "list",
    "allowed_values_ref": "payload_options.pipeline_inspection"
}

catalog_stage1 = builder._resolve_candidate_catalog(field_def, "pipeline_inspection", task_state_stage1)
candidates_stage1 = [c["canonical_value"] for c in catalog_stage1]

print("\n【阶段 1】已确定任务为 [管缆巡检]，未确定具体机器人:")
print(f"-> 系统推荐/展示的候选载荷工具 (共 {len(candidates_stage1)} 项):")
print(f"   {candidates_stage1}")


# 场景 2：确定了具体机器人型号为 [水下无人自主航行器 324CC] (autonomous_underwater_vehicle_324cc)
task_state_stage2_auv = {
    "task_type_key": "pipeline_inspection",
    "equipment_type": "autonomous_underwater_vehicle_324cc"
}

catalog_stage2_auv = builder._resolve_candidate_catalog(field_def, "pipeline_inspection", task_state_stage2_auv)
candidates_stage2_auv = [c["canonical_value"] for c in catalog_stage2_auv]

print("\n【阶段 2A】确定机器人型号为 [水下无人自主航行器 324CC (AUV)]:")
print(f"-> 系统只提供该 AUV 真实支持的 supported_payloads 选配工具 (共 {len(candidates_stage2_auv)} 项):")
print(f"   {candidates_stage2_auv}")


# 场景 3：确定了具体机器人型号为 [轻型工作级深海机器人 150HP] (light_work_class_rov_150hp)
task_state_stage2_lrov = {
    "task_type_key": "pipeline_inspection",
    "equipment_type": "light_work_class_rov_150hp"
}

catalog_stage2_lrov = builder._resolve_candidate_catalog(field_def, "pipeline_inspection", task_state_stage2_lrov)
candidates_stage2_lrov = [c["canonical_value"] for c in catalog_stage2_lrov]

print("\n【阶段 2B】确定机器人型号为 [轻型工作级深海机器人 150HP (天鹰座)]:")
print(f"-> 系统只提供该天鹰座 150HP 真实支持的 supported_payloads 选配工具 (共 {len(candidates_stage2_lrov)} 项):")
print(f"   {candidates_stage2_lrov}")
