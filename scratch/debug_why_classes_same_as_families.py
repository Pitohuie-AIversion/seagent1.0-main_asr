from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.dialogue_manager import DialogueManager

kb = KnowledgeBase()
builder = OutputBuilder(kb)
dm = DialogueManager()

print("--- 1. kb.list_robot_classes('pipeline_inspection') ---")
print(kb.list_robot_classes("pipeline_inspection"))

print("\n--- 2. builder.resolve_allowed_values for robot_classes ---")
print(builder.resolve_allowed_values({"allowed_values_ref": "robot_classes"}, "pipeline_inspection", {}))

print("\n--- 3. builder.resolve_allowed_values for robot_family_full_names ---")
print(builder.resolve_allowed_values({"allowed_values_ref": "robot_family_full_names"}, "pipeline_inspection", {}))

print("\n--- 4. builder._resolve_candidate_catalog for equipment_class ---")
print(builder._resolve_candidate_catalog({"key": "equipment_class", "allowed_values_ref": "robot_classes"}, "pipeline_inspection", {}))

print("\n--- 5. builder._resolve_candidate_catalog for equipment_family ---")
print(builder._resolve_candidate_catalog({"key": "equipment_family", "allowed_values_ref": "robot_family_full_names"}, "pipeline_inspection", {}))

print("\n--- 6. dm.slot_store.get_missing_slots ---")
dm.task_state = {"task_type_key": "pipeline_inspection"}
dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
dm.slot_store.slots["task_type_key"].status = "valid"
user_req_schema = [
    {"key": "equipment_class", "label": "机器人类别", "type": "string", "allowed_values_ref": "robot_classes"},
    {"key": "equipment_family", "label": "作业机器人系列", "type": "string", "allowed_values_ref": "robot_family_full_names"}
]
missing = dm.slot_store.get_missing_slots(
    user_req_schema,
    allowed_values_resolver=lambda f: builder.resolve_allowed_values(f, "pipeline_inspection", dm.task_state)
)
for m in missing:
    print(m["key"], "-> allowed_values:", m.get("allowed_values"))
