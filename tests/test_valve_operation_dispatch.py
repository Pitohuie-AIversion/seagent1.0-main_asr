"""Preserve valve intent direction and never transmit withdrawal as INSERT_PLUG."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mcp.core.rosbridge_client import RosbridgeClient, intent_to_syscmd
from mcp.core.sealien_protocol import ProtocolValidationError, validate_valve_operation
from mcp.mock.seagent_mcp_adapter import SeagentROS2MCPAdapter
from src.dialogue_manager import DialogueManager
from src.dispatch.task_intent_builder import TaskIntentBuilder, validate_task_intent
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM


def valve_intent(operation='insert'):
    return {'schema_version': 2, 'task_type': 'valve_operation', 'priority': 7,
            'location': {'water_depth_m': 300},
            'task': {'type': 'valve_operation', 'details': {
                'operation': operation, 'target': {'latitude': 20.815, 'longitude': 115.735}}}}


@pytest.mark.parametrize('operation', ['withdraw', None, 'unknown', '', False])
def test_unsupported_or_missing_action_never_reaches_transport(operation):
    intent = valve_intent(operation)
    client = RosbridgeClient()
    client.publish = Mock()
    with pytest.raises(ProtocolValidationError):
        client.publish_task_cmd(intent, task_id=0x80001)
    client.publish.assert_not_called()


def test_legacy_generic_task_without_operation_does_not_default_to_insert():
    intent = valve_intent()
    del intent['task']['details']['operation']
    with pytest.raises(ProtocolValidationError, match='未明确插入或拔出'):
        intent_to_syscmd(intent, task_id=0x80001)


def test_explicit_legacy_insertion_label_remains_compatible():
    intent = valve_intent()
    intent['task_type'] = intent['task']['type'] = '采油树控制面板插入'
    del intent['task']['details']['operation']
    cmd = intent_to_syscmd(intent, task_id=0x80001).to_dict()
    assert cmd['task_type'] == 4 and cmd['params'] == []


def test_conflicting_label_and_operation_are_rejected():
    intent = valve_intent('insert')
    intent['task_type'] = '采油树控制面板拔出'
    with pytest.raises(ProtocolValidationError, match='不一致'):
        intent_to_syscmd(intent, task_id=0x80001)


def test_mock_adapter_cannot_bypass_withdrawal_guard(monkeypatch):
    adapter = SeagentROS2MCPAdapter()
    start = Mock(side_effect=AssertionError('mock transport must not start'))
    monkeypatch.setattr(adapter, '_get_server_params', start)
    with pytest.raises(ProtocolValidationError, match='未定义.*拔出'):
        asyncio.run(adapter.dispatch_task_intent(valve_intent('withdraw')))
    start.assert_not_called()


@pytest.mark.parametrize('subtype,operation,dispatch_state', [
    ('采油树控制面板插入', 'insert', 'SCHEDULED'),
    ('采油树控制面板拔出', 'withdraw', 'BLOCKED'),
])
def test_real_confirmation_archive_and_ros_command_preserve_action(tmp_path, monkeypatch, subtype, operation, dispatch_state):
    import web_backend
    import src.web.state as state
    from src.temporal.simulated_time import get_current_datetime

    monkeypatch.setenv('SEAGENT_RESULT_DIR', str(tmp_path))
    manager = DialogueManager(ScriptedLLM(), KnowledgeBase())
    manager.session_id = 'valve-operation-' + operation
    manager.slot_store.init_task_slots(manager.builder.get_schema('tree_valve_operation', 'normal'))
    tomorrow = get_current_datetime() + timedelta(days=2)
    start = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    values = {'internal_id': str(uuid4()), 'intent_id': f'TI{start:%Y%m%d}01',
              'task_type_key': 'tree_valve_operation', 'task_type': subtype,
              'equipment_family': '通用工作级深海机器人',
              'equipment_type': '通用工作级深海机器人 250HP', 'equipment_unit_id': 'WROV-250-001',
              'oilfield_name': '流花11-1油田', 'oilfield_entity_id': 'liuhua_11_1',
              'oilfield_coordinates': {'lat': 20.815, 'lon': 115.735},
              'water_depth': 300, 'wellhead_id': 'LH11-A2',
              'payload': ['液压飞线插拔工具'], 'support_vessel': '海洋石油681',
              'start_time': start.isoformat(), 'end_time': (start + timedelta(hours=3)).isoformat()}
    for key, value in values.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status='valid')
    manager._rebuild_cache()
    validation = manager._refresh_validation(purpose='preview')
    manager._blocking_violations = list(validation.violations)
    manager.phase = 'blocked_soft'
    manager.process('忽略软警告')
    assert manager.phase == 'confirming'
    monkeypatch.setitem(state._sessions_manager, manager.session_id, manager)
    monkeypatch.setitem(web_backend.app.config, 'SEAGENT_API_TOKENS', [])
    bridge = Mock()
    bridge.get_dispatch_record.return_value = None
    monkeypatch.setattr(state, '_shared_mcp_bridge', bridge)

    response = web_backend.app.test_client().post('/api/chat', json={
        'session_id': manager.session_id, 'message': '确认发布'}).get_json()

    assert response['done'] and response['ui_state']['phase'] == 'done', response['reply']
    assert response['ros2_dispatch']['state'] == dispatch_state
    bridge.dispatch_intent.assert_not_called()
    artifact = json.loads(next((tmp_path / 'task').glob('task_intent_*.json')).read_text())
    history = json.loads(next((tmp_path / 'history').glob('history_*.json')).read_text())
    assert artifact['task_type'] == 'valve_operation'
    assert artifact['task']['details']['operation'] == operation
    assert history['task_state']['task_type'] == subtype
    assert history['ros2_dispatch']['state'] == dispatch_state
    assert validate_task_intent(artifact, manager.kb.task_schemas)
    client = RosbridgeClient()
    client.publish = Mock()
    if operation == 'insert':
        client.publish_task_cmd(artifact, task_id=0x80001)
        command = client.publish.call_args.args[2]
        assert command['task_type'] == 4
        assert command['params'] == [] and command['frame_id'] == ''
        assert command['pos_target'][0]['position']['z'] == -300
    else:
        assert '协议未定义' in response['reply'] and '拔出' in response['reply']
        assert '尚未下发' in response['reply']
        assert '到期后请检查执行条件并下发' not in response['reply']
        assert response['ros2_dispatch']['retry_allowed'] is False
        with pytest.raises(ProtocolValidationError, match='未定义.*拔出'):
            client.publish_task_cmd(artifact, task_id=0x80001)
        client.publish.assert_not_called()
