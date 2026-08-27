from src.intent_router import IntentRouter


class _ClarifyLLM:
    def classify_interaction(self, messages, max_tokens=480, role=None):
        return {
            "schema_version": 1,
            "operation": "CLARIFY",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "CLARIFICATION",
            "needs_clarification": True,
            "clarification_reason": "multiple options require a unique choice",
            "confidence": 0.86,
        }


def test_expected_list_field_multi_candidate_selection_corrects_to_write():
    router = IntentRouter(_ClarifyLLM())

    route = router.route(
        "高清水下摄像机、激光标尺",
        conversation_history=[],
        task_state={"task_type_key": "pipeline_inspection"},
        expected_slots=["inspection_tools"],
        expected_slot_options=[
            {
                "key": "inspection_tools",
                "label": "巡检工具",
                "type": "list",
                "allowed_values": ["高清水下摄像机", "激光标尺", "机械扫描声呐"],
            }
        ],
    )

    assert route.interaction_type == "WRITE"
    assert route.dialogue_mode == "task_collection"
    assert route.query_intent is None
    assert route.interaction_plan.reason_code == "EXPECTED_LIST_SELECTION_WRITE_CORRECTION"
