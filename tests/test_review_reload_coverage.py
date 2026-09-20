"""A reload must update changed code or explicitly require a restart."""

import importlib
import os
import shutil
import sys

import pytest

import src
from src import hot_reload


@pytest.mark.parametrize("relative", [
    "handlers/slot_extraction_pipeline.py", "temporal/relative_time_parser.py",
    "web/routes_chat.py", "knowledge/base.py",
    "dispatch/task_intent_builder.py", "extraction/normalizer.py", "dialogue_manager.py",
])
def test_unsupported_changes_fail_before_mutating_modules(monkeypatch, relative):
    reloads = []
    monkeypatch.setattr(importlib, "reload", lambda mod: reloads.append(mod))
    ok, message, modules = hot_reload.perform_reload([str(hot_reload.SRC_DIR / relative)])
    assert not ok
    assert "需要重启" in message
    assert relative in message
    assert modules == reloads == []


def test_force_reload_cannot_bypass_coverage_check(monkeypatch):
    monkeypatch.setattr(hot_reload, "check_changed_files", lambda: [
        str(hot_reload.SRC_DIR / "handlers/slot_extraction_pipeline.py"),
    ])
    assert hot_reload.force_reload()["ok"] is False


def test_changed_source_with_a_cached_consumer_requires_restart(monkeypatch, tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    path = source_dir / "review_reload_probe.py"
    path.write_text("def value():\n    return 'old'\n", encoding="utf-8")
    monkeypatch.setattr(src, "__path__", [*src.__path__, str(source_dir)])
    module_name = "src.review_reload_probe"
    module = importlib.import_module(module_name)
    assert module.value() == "old"
    cached_consumer = module.value
    try:
        monkeypatch.setattr(hot_reload, "SRC_DIR", source_dir)
        monkeypatch.setattr(hot_reload, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(hot_reload, "_file_mtimes", {})
        monkeypatch.delitem(sys.modules, "web_backend", raising=False)
        previous_mtime = path.stat().st_mtime
        path.write_text("def value():\n    return 'updated behavior'\n", encoding="utf-8")
        os.utime(path, (previous_mtime + 2, previous_mtime + 2))
        importlib.invalidate_caches()
        ok, message, modules = hot_reload.perform_reload([str(path)])
        assert not ok and "需要重启" in message
        assert modules == []
        assert module.value() == cached_consumer() == "old"
        # The changed implementation exists, but only replacing the module
        # would leave the consumer on old code. Never label that a success.
        importlib.reload(module)
        assert module.value() == "updated behavior"
        assert cached_consumer() == "old"
    finally:
        sys.modules.pop(module_name, None)
        if hasattr(src, "review_reload_probe"):
            delattr(src, "review_reload_probe")


def test_real_config_change_refreshes_data_without_changing_class_identity(monkeypatch, tmp_path):
    import yaml
    import web_backend
    import src.knowledge.models as models
    from src.knowledge_retriever import KnowledgeBase

    config_dir = tmp_path / "config"
    shutil.copytree(hot_reload.CONFIG_DIR, config_dir)
    monkeypatch.setattr(models, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(web_backend, "_sessions_manager", {})
    original = KnowledgeBase()
    monkeypatch.setattr(web_backend, "_shared_kb", original)
    monkeypatch.setattr(hot_reload, "_file_mtimes", {})
    path = config_dir / "assets.yaml"
    assets = yaml.safe_load(path.read_text(encoding="utf-8"))
    assets["review_probe"] = "new configuration"
    path.write_text(yaml.safe_dump(assets, allow_unicode=True), encoding="utf-8")
    ok, message, modules = hot_reload.perform_reload([str(path)])
    assert ok, message
    assert modules == []
    assert "review_probe" not in original.assets
    assert web_backend._shared_kb.assets["review_probe"] == "new configuration"
    assert type(web_backend._shared_kb) is type(original)


def test_startup_only_configuration_requires_restart():
    ok, message, modules = hot_reload.perform_reload([str(hot_reload.CONFIG_DIR / "asr.yaml")])
    assert not ok and "需要重启" in message
    assert modules == []
