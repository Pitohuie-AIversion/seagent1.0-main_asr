"""tests/test_dialogue_manager_delegation.py

验证 DialogueManager 动态委托路由与别名分发机制的正确性与向后兼容性。
"""

import pytest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.validator import Violation


@pytest.fixture
def dm():
    return DialogueManager()


def test_delegate_slot_handler_aliases(dm):
    """测试 slot_handler 相关别名和同名方法的动态委托。"""
    # 别名: _auto_collapse_robot_cascade -> slot_handler.auto_collapse_robot_cascade
    assert hasattr(dm, "_auto_collapse_robot_cascade")
    assert callable(dm._auto_collapse_robot_cascade)
    
    # 别名: _source_for_resolution_method -> slot_handler.source_for_resolution_method
    assert hasattr(dm, "_source_for_resolution_method")
    assert callable(dm._source_for_resolution_method)
    
    # 别名: _scope_confirmed_recommendation -> slot_handler.scope_confirmed_recommendation
    assert hasattr(dm, "_scope_confirmed_recommendation")
    assert callable(dm._scope_confirmed_recommendation)


def test_delegate_constraint_handler_aliases(dm):
    """测试 constraint_handler 相关别名和方法的动态委托。"""
    # 别名: _run_constraint_check -> constraint_handler.run_constraint_check
    assert hasattr(dm, "_run_constraint_check")
    assert callable(dm._run_constraint_check)

    # 别名: _is_whitelisted -> constraint_handler.is_whitelisted
    assert hasattr(dm, "_is_whitelisted")
    assert callable(dm._is_whitelisted)
    v = Violation(constraint_id="C001", constraint_name="depth_limit", message="test", severity="soft")
    assert dm._is_whitelisted(v) is False

    # 别名: _invalidate_whitelist -> constraint_handler.invalidate_whitelist
    assert hasattr(dm, "_invalidate_whitelist")
    assert callable(dm._invalidate_whitelist)


def test_delegate_router_handler_methods(dm):
    """测试 router_handler 相关方法的动态委托。"""
    # 同名/别名: _build_grounded_recommendation
    assert hasattr(dm, "_build_grounded_recommendation")
    assert callable(dm._build_grounded_recommendation)

    # 同名方法: _handle_general_chat
    assert hasattr(dm, "_handle_general_chat")
    assert callable(dm._handle_general_chat)

    # 同名方法: _handle_knowledge_query
    assert hasattr(dm, "_handle_knowledge_query")
    assert callable(dm._handle_knowledge_query)

    # 同名方法: _is_environment_status_query
    assert hasattr(dm, "_is_environment_status_query")
    assert callable(dm._is_environment_status_query)


def test_delegate_special_accessors(dm):
    """测试特殊访问器委托: get_final_result, is_start_time_near_now, _ensure_payload_guidance 等。"""
    # get_final_result 返回 final_result
    assert dm.get_final_result() is None
    dm.final_result = {"status": "ok"}
    assert dm.get_final_result() == {"status": "ok"}

    # is_start_time_near_now
    assert hasattr(dm, "is_start_time_near_now")
    assert callable(dm.is_start_time_near_now)

    # _ensure_payload_guidance
    assert hasattr(dm, "_ensure_payload_guidance")
    assert callable(dm._ensure_payload_guidance)


def test_delegate_patch_object_compatibility(dm):
    """测试 patch.object 针对动态委托属性的兼容性。"""
    with patch.object(dm, "_auto_collapse_robot_cascade", return_value={"collapsed": True}):
        result = dm._auto_collapse_robot_cascade()
        assert result == {"collapsed": True}


def test_retained_class_level_methods():
    """测试被外部测试显式 patch 的 4 个类方法与 4 个实例方法必须在类中显式定义。"""
    # 类级别 patch
    assert hasattr(DialogueManager, "process")
    assert hasattr(DialogueManager, "_apply_updates_in_transaction")
    assert hasattr(DialogueManager, "_handle_equipment_updates_in_transaction")
    assert hasattr(DialogueManager, "_handle_task_type_update_in_transaction")

    # 实例级别 patch
    assert hasattr(DialogueManager, "_refresh_validation")
    assert hasattr(DialogueManager, "refresh_external_state_constraints")
    assert hasattr(DialogueManager, "_handle_final_publish_confirmation")
    assert hasattr(DialogueManager, "_handle_soft_warning_confirmation")


def test_nonexistent_attribute_raises_error(dm):
    """测试访问不存在的属性正确抛出 AttributeError。"""
    with pytest.raises(AttributeError, match="object has no attribute 'nonexistent_foo_bar'"):
        _ = dm.nonexistent_foo_bar
