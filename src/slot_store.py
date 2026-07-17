import copy
import logging
import threading
from typing import Any, Optional, Dict, List, Tuple
from src.simulated_time import get_current_datetime

logger = logging.getLogger("backend.slot_store")


class SlotVersionConflict(RuntimeError):
    """Raised when commit_transaction detects a store version mismatch."""
    pass


BASE_SLOT_TYPES = {
    "task_type": "string",
    "task_type_key": "string",
    "emergency_mode": "boolean",
    "task_id": "string",
    "intent_id": "string",
    "equipment_name": "string",
    "equipment_type": "string",
}

ALLOWED_INTERNAL_SLOTS = {
    "raw_oilfield_name",
    "oilfield_match_status",
    "oilfield_match_confidence",
    "oilfield_match_evidence",
    "oilfield_match_candidates",
    "oilfield_entity_id",
    "pending_oilfield_name",
    "pending_oilfield_candidates",
    "_rov_candidates",
}


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
        self.updated_at = updated_at or get_current_datetime().isoformat()
        self.version = version
        self.candidate_value = candidate_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot_name": self.slot_name,
            "value": copy.deepcopy(self.value),
            "value_type": self.value_type,
            "status": self.status,
            "source": self.source,
            "raw_value": copy.deepcopy(self.raw_value),
            "confidence": self.confidence,
            "validation_error": self.validation_error,
            "updated_at": self.updated_at,
            "version": self.version,
            "candidate_value": copy.deepcopy(self.candidate_value)
        }

    def copy(self):
        return Slot(
            slot_name=self.slot_name,
            value=copy.deepcopy(self.value),
            value_type=self.value_type,
            status=self.status,
            source=self.source,
            raw_value=copy.deepcopy(self.raw_value),
            confidence=self.confidence,
            validation_error=self.validation_error,
            updated_at=self.updated_at,
            version=self.version,
            candidate_value=copy.deepcopy(self.candidate_value)
        )


class SlotStore:
    def __init__(self, kb=None):
        self.kb = kb
        self._lock = threading.RLock()
        self.slots: Dict[str, Slot] = {}
        self.unresolved: List[Any] = []
        self.version: int = 0
        self._initialize_base_slots()

    def _initialize_base_slots(self):
        with self._lock:
            base_keys = {
                "task_type": "string",
                "task_type_key": "string",
                "emergency_mode": "boolean",
                "task_id": "string",
                "intent_id": "string",
                "equipment_name": "string",
                "equipment_type": "string",
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
        with self._lock:
            self._initialize_base_slots()
            schema_keys = {field["key"] for field in schema_fields}

            # Clean up dynamic slots from previous tasks
            to_remove = [
                k for k in self.slots
                if k not in BASE_SLOT_TYPES and k not in schema_keys and k not in ALLOWED_INTERNAL_SLOTS
            ]
            for k in to_remove:
                del self.slots[k]

            for field in schema_fields:
                key = field["key"]
                ftype = field.get("type", "string")
                if key in self.slots:
                    if self.slots[key].value_type != ftype:
                        self.slots[key].value_type = ftype
                        self.slots[key].value = None
                        self.slots[key].candidate_value = None
                        self.slots[key].status = "missing"
                else:
                    self.slots[key] = Slot(slot_name=key, value_type=ftype)

    def get_task_state(self) -> Dict[str, Any]:
        """Returns ONLY status == 'valid' and non-None slots as current facts."""
        with self._lock:
            return {
                key: copy.deepcopy(slot.value)
                for key, slot in self.slots.items()
                if slot.status == "valid" and slot.value is not None
            }

    def get_slot_snapshot(self) -> Dict[str, Any]:
        """Returns full status dictionary of all slots."""
        with self._lock:
            return {
                key: copy.deepcopy(slot.to_dict())
                for key, slot in self.slots.items()
            }

    def get_built_json(self) -> Dict[str, Any]:
        with self._lock:
            return {
                key: copy.deepcopy(slot.value)
                for key, slot in self.slots.items()
                if slot.status == "valid" and slot.value is not None
            }

    def get_missing_slots(self, required_schema: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            missing = []
            for field in required_schema:
                key = field["key"]
                slot = self.slots.get(key)
                if not slot or slot.status != "valid" or slot.value is None:
                    missing.append(field)
            return missing

    def export_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "store_version": self.version,
                "slots": {
                    key: slot.to_dict()
                    for key, slot in self.slots.items()
                },
                "unresolved": copy.deepcopy(self.unresolved),
            }

    def restore_snapshot(self, snapshot: Dict[str, Any]):
        with self._lock:
            if not isinstance(snapshot, dict):
                return
            self.version = snapshot.get("store_version", 0)
            slots_data = snapshot.get("slots", {})
            new_slots = {}
            for key, sdict in slots_data.items():
                if isinstance(sdict, dict):
                    new_slots[key] = Slot(
                        slot_name=sdict.get("slot_name", key),
                        value=copy.deepcopy(sdict.get("value")),
                        value_type=sdict.get("value_type", "string"),
                        status=sdict.get("status", "missing"),
                        source=sdict.get("source", "user_input"),
                        raw_value=copy.deepcopy(sdict.get("raw_value")),
                        confidence=sdict.get("confidence"),
                        validation_error=sdict.get("validation_error"),
                        updated_at=sdict.get("updated_at"),
                        version=sdict.get("version", 0),
                        candidate_value=copy.deepcopy(sdict.get("candidate_value"))
                    )
                elif isinstance(sdict, Slot):
                    new_slots[key] = sdict.copy()
            self.slots = new_slots
            self.unresolved = copy.deepcopy(snapshot.get("unresolved", []))

    @classmethod
    def from_snapshot(cls, snapshot: Dict[str, Any], kb=None):
        store = cls(kb)
        store.restore_snapshot(snapshot)
        return store

    def clone_slots(self) -> Dict[str, Slot]:
        with self._lock:
            return {k: s.copy() for k, s in self.slots.items()}

    def snapshot(self) -> Tuple[Dict[str, Slot], List[Any], int]:
        with self._lock:
            return self.clone_slots(), copy.deepcopy(self.unresolved), self.version

    def commit_transaction(
        self,
        new_slots: Dict[str, Slot],
        new_unresolved: List[Any],
        request_id: str = "req_default",
        expected_version: Optional[int] = None,
    ):
        with self._lock:
            if expected_version is not None and expected_version != self.version:
                raise SlotVersionConflict(
                    f"SlotStore version conflict: expected version {expected_version}, "
                    f"but current store version is {self.version}"
                )

            temp_slots = {k: s.copy() for k, s in new_slots.items()}
            temp_unresolved = copy.deepcopy(new_unresolved)

            now_str = get_current_datetime().isoformat()
            task_id = self.slots.get("task_id").value if (self.slots.get("task_id") and self.slots.get("task_id").value) else "unknown"

            slot_changes_detected = False

            # Check deleted slots
            deleted_keys = set(self.slots.keys()) - set(temp_slots.keys())
            for key in deleted_keys:
                old_slot = self.slots[key]
                slot_changes_detected = True
                logger.info(
                    f"[SLOT_DELETE] task_id={task_id} request_id={request_id} "
                    f"store_version={self.version} slot_name={key} "
                    f"old_value={old_slot.value} old_status={old_slot.status} action=delete"
                )

            for key, new_slot in temp_slots.items():
                old_slot = self.slots.get(key)
                has_changed = False

                if not old_slot:
                    has_changed = True
                    old_val = None
                    old_status = "non_existent"
                    new_slot.version = 1
                    new_slot.updated_at = now_str
                else:
                    old_val = old_slot.value
                    old_status = old_slot.status
                    if (
                        old_slot.value != new_slot.value
                        or old_slot.value_type != new_slot.value_type
                        or old_slot.status != new_slot.status
                        or old_slot.source != new_slot.source
                        or old_slot.raw_value != new_slot.raw_value
                        or old_slot.confidence != new_slot.confidence
                        or old_slot.validation_error != new_slot.validation_error
                        or old_slot.candidate_value != new_slot.candidate_value
                    ):
                        has_changed = True
                        new_slot.version = old_slot.version + 1
                        new_slot.updated_at = now_str
                    else:
                        new_slot.version = old_slot.version
                        new_slot.updated_at = old_slot.updated_at

                if has_changed:
                    slot_changes_detected = True
                    logger.info(
                        f"[SLOT_UPDATE] task_id={task_id} request_id={request_id} "
                        f"store_version={self.version} slot_name={key} "
                        f"old_value={old_val} new_value={new_slot.value} "
                        f"old_status={old_status} new_status={new_slot.status} "
                        f"source={new_slot.source}"
                    )

            unresolved_changed = (self.unresolved != temp_unresolved)
            if unresolved_changed:
                logger.info(
                    f"[UNRESOLVED_UPDATE] task_id={task_id} request_id={request_id} "
                    f"store_version={self.version} old_unresolved={self.unresolved} "
                    f"new_unresolved={temp_unresolved}"
                )

            if slot_changes_detected or unresolved_changed:
                self.slots = temp_slots
                self.unresolved = temp_unresolved
                self.version += 1
