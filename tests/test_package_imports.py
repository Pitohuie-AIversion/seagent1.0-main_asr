"""Lightweight utilities must not require the dialogue/model stack to import."""

import importlib
from pathlib import Path
import subprocess
import sys

import pytest


def test_lightweight_imports_do_not_load_dialogue_or_model_dependencies():
    code = """
import importlib
import sys

for name in ('src', 'src.dispatch.result_paths', 'src.state_info'):
    importlib.import_module(name)
    unexpected = set(sys.modules) & {
        'src.dialogue_manager', 'src.llm_client', 'torch', 'vllm', 'transformers',
    }
    assert not unexpected, (name, unexpected)

import src
assert set(src.__all__) <= set(dir(src))
assert 'src.dialogue_manager' not in sys.modules
try:
    src.nonexistent_export
except AttributeError:
    pass
else:
    raise AssertionError('Unknown exports must raise AttributeError')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("dialogue_manager", ("DialogueManager",)),
        ("session.intent_router", ("IntentRouter",)),
        ("knowledge_retriever", ("KnowledgeBase",)),
        ("llm_client", ("LLMClient",)),
        ("extraction.normalizer", ("FieldNormalizer",)),
        ("dispatch.output_builder", ("OutputBuilder",)),
        ("slots.slot_store", ("Slot", "SlotStore")),
        ("dispatch.task_intent_builder", ("TaskIntentBuilder", "TaskPublishLock")),
        ("validation.validator", ("TaskValidator", "ValidationResult", "Violation")),
    ],
)
def test_public_exports_are_the_original_objects(module, names):
    import src

    namespace = {}
    exec(f"from src import {', '.join(names)}", namespace)
    original_module = importlib.import_module(f"src.{module}")
    for name in names:
        assert namespace[name] is getattr(original_module, name)
        assert getattr(src, name) is namespace[name]
        assert name in src.__all__
