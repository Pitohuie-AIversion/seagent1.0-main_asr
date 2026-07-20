import datetime
import logging
from typing import Any, Callable, Optional, Dict, List

logger = logging.getLogger("backend.slot_store")

class Slot:
    def __init__(
        self,
        slot_name: str,
        value: Any = None,
        value_type: str = "string",
        status: str = "missing",
        source: str = "user_input",
        raw_value: Any = None,
        confidence: Optional[float] = None,
        validation_error: Optional[str] = None,
        updated_at: Optional[str] = None,
        version: int = 0,
        candidate_value: Any = None
    ):
        self.slot_name = slot_name
        self.value = value
        self.value_type = value_type
        self.status = status  # missing | candidate | valid | invalid | conflict | unresolved
        self.source = source  # user_input | auto | fixed
        self.raw_value = raw_value
        self.confidence = confidence
        self.validation_error = validation_error
        self.updated_at = updated_at or datetime.datetime.now().isoformat()
        self.version = version
        self.candidate_value = candidate_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot_name": self.slot_name,
            "value": self.value,
            "value_type": self.value_type,
            "status": self.status,
            "source": self.source,
            "raw_value": self.raw_value,
            "confidence": self.confidence,
            "validation_error": self.validation_error,
            "updated_at": self.updated_at,
            "version": self.version,
            "candidate_value": self.candidate_value
        }

    def copy(self):
        return Slot(
            slot_name=self.slot_name,
            value=self.value,
            value_type=self.value_type,
            status=self.status,
            source=self.source,
            raw_value=self.raw_value,
            confidence=self.confidence,
            validation_error=self.validation_error,
            updated_at=self.updated_at,
            version=self.version,
            candidate_value=self.candidate_value
        )


class SlotStore:
    def __init__(self, kb=None):
        self.kb = kb
        self.slots: Dict[str, Slot] = {}
        self.unresolved: List[Any] = []
        self.version = 0
        self._initialize_base_slots()

    def _initialize_base_slots(self):
        # Base generic slots always present in tasks
        base_keys = {
            "task_type": "string",
            "task_type_key": "string",
            "emergency_mode": "boolean",
            "task_id": "string",
            "intent_id": "string",
            "equipment_name": "string",
            "raw_oilfield_name": "string",
            "oilfield_match_status": "string",
            "oilfield_match_confidence": "number",
            "oilfield_match_evidence": "list",
            "oilfield_match_candidates": "list",
            "pending_oilfield_name": "string",
            "pending_oilfield_candidates": "list",
            "_rov_candidates": "list"
        }
        for key, vtype in base_keys.items():
            if key not in self.slots:
                self.slots[key] = Slot(slot_name=key, value_type=vtype)

    def init_task_slots(self, schema_fields: List[Dict[str, Any]]):
        self._initialize_base_slots()
        for field in schema_fields:
            key = field["key"]
            ftype = field.get("type", "string")
            if key in self.slots:
                self.slots[key].value_type = ftype
            else:
                self.slots[key] = Slot(slot_name=key, value_type=ftype)

    def get_task_state(self) -> Dict[str, Any]:
        # Derives the task_state dict from SlotStore.
        state = {}
        for key, slot in self.slots.items():
            if slot.value is not None and slot.status in ("valid", "candidate", "invalid", "conflict"):
                state[key] = slot.value
        return state

    def get_built_json(
        self,
        output_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """返回有效槽位；提供输出 schema 时仅投影正式任务字段。"""
        built = {}
        keys = (
            [field["key"] for field in output_schema]
            if output_schema is not None
            else self.slots.keys()
        )
        for key in keys:
            slot = self.slots.get(key)
            if slot is None:
                continue
            if slot.value is not None and slot.status == "valid":
                built[key] = slot.value
        return built

    def get_missing_slots(
        self,
        required_schema: List[Dict[str, Any]],
        allowed_values_resolver: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """返回缺失字段，并按需补充与当前任务状态相关的合法候选值。"""
        missing = []
        for field in required_schema:
            key = field["key"]
            slot = self.slots.get(key)
            if not slot or slot.status != "valid" or slot.value is None:
                # Schema 是知识库中的共享配置，不能在收集过程中原地修改。
                missing_field = dict(field)
                if allowed_values_resolver is not None:
                    missing_field["allowed_values"] = list(
                        allowed_values_resolver(field) or []
                    )
                missing.append(missing_field)
        return missing

    def clone_slots(self) -> Dict[str, Slot]:
        return {k: s.copy() for k, s in self.slots.items()}

    def commit_transaction(self, new_slots: Dict[str, Slot], new_unresolved: List[Any], request_id: str = "req_default"):
        from src.simulated_time import get_current_datetime
        now_str = get_current_datetime().isoformat()
        
        task_id = self.slots.get("task_id").value if self.slots.get("task_id") else "unknown"

        for key, new_slot in new_slots.items():
            old_slot = self.slots.get(key)
            has_changed = False
            
            if not old_slot:
                has_changed = True
                old_val = None
                new_slot.version = 1
                new_slot.updated_at = now_str
            else:
                old_val = old_slot.value
                if (old_slot.value != new_slot.value or 
                    old_slot.status != new_slot.status or 
                    old_slot.validation_error != new_slot.validation_error):
                    has_changed = True
                    new_slot.version = old_slot.version + 1
                    new_slot.updated_at = now_str
                else:
                    new_slot.version = old_slot.version
                    new_slot.updated_at = old_slot.updated_at

            if has_changed:
                logger.info(
                    f"[SLOT_UPDATE] task_id={task_id} request_id={request_id} "
                    f"slot_name={key} old_value={old_val} new_value={new_slot.value} "
                    f"status={new_slot.status} source={new_slot.source}"
                )
                print(
                    f"📝 [SLOT_UPDATE] key={key} old={old_val} new={new_slot.value} "
                    f"status={new_slot.status} source={new_slot.source}"
                )

        self.slots = new_slots
        self.unresolved = new_unresolved
        self.version += 1
