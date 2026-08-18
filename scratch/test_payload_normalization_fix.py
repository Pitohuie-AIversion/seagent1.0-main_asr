from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder

kb = KnowledgeBase()
builder = OutputBuilder(kb)

task_state = {
    "task_type_key": "pipeline_inspection",
    "equipment_class": "light_work_class_rov",
    "equipment_family": "light_work_class_rov",
    "equipment_type": "轻型工作级深海机器人150HP",
    "equipment_unit_id": "LROV-150-001",
}

field_def = {"key": "payload"}
allowed = builder.resolve_allowed_values(field_def, "pipeline_inspection", task_state)
print("=== Allowed Payloads for LROV-150-001 ===")
for p in allowed:
    print(" -", p)
