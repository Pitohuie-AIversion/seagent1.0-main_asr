import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.dispatch.result_paths import get_result_dir, get_task_dir, get_history_dir
from tests.runtime_isolation import configure_test_artifact_paths


class TestAuditSecurityBoundaries(unittest.TestCase):
    """验证安全隔离与存储边界（针对审计报告 P1-4 与 P1-5 的回归防护）"""

    def setUp(self):
        self._env_backup = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp(prefix="seagent-audit-test-")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────────────
    # P1-4: 测试运行态与状态文件隔离边界
    # ──────────────────────────────────────────────────────────────────────────

    def test_inherited_state_file_is_isolated_and_cloned(self):
        """父进程显式设置 SEAGENT_STATE_FILE 时，测试环境仍必须创建沙箱副本，绝不能直写原文件。"""
        real_state_file = Path(self.temp_dir) / "parent_real_state.yaml"
        real_state_file.write_text("telemetry: {battery: 100}\n", encoding="utf-8")
        os.environ["SEAGENT_STATE_FILE"] = str(real_state_file)

        test_dir = Path(self.temp_dir) / "test_sandbox"
        os.environ["SEAGENT_TEST_RESULT_DIR"] = str(test_dir)

        configured_root = configure_test_artifact_paths()

        active_state = Path(os.environ["SEAGENT_STATE_FILE"])
        self.assertNotEqual(active_state, real_state_file)
        self.assertEqual(active_state.parent, configured_root)
        self.assertTrue(active_state.exists())
        self.assertIn("battery: 100", active_state.read_text(encoding="utf-8"))

    def test_custom_runtime_result_dir_collision_is_rejected(self):
        """测试目录与用户自定义运行目录相同时必须明确报错拒绝。"""
        custom_run_dir = Path(self.temp_dir) / "custom_runtime"
        custom_run_dir.mkdir(parents=True, exist_ok=True)

        os.environ["SEAGENT_RESULT_DIR"] = str(custom_run_dir)
        os.environ["SEAGENT_TEST_RESULT_DIR"] = str(custom_run_dir)

        with self.assertRaises(RuntimeError) as ctx:
            configure_test_artifact_paths()
        self.assertIn("must not point to the runtime result directory", str(ctx.exception))

    def test_custom_runtime_result_dir_overlap_is_rejected(self):
        """测试目录若处于自定义运行目录内部或包含自定义运行目录，必须明确报错拒绝。"""
        custom_run_dir = Path(self.temp_dir) / "custom_runtime"
        custom_run_dir.mkdir(parents=True, exist_ok=True)
        sub_test_dir = custom_run_dir / "nested_test"

        os.environ["SEAGENT_RESULT_DIR"] = str(custom_run_dir)
        os.environ["SEAGENT_TEST_RESULT_DIR"] = str(sub_test_dir)

        with self.assertRaises(RuntimeError) as ctx:
            configure_test_artifact_paths()
        self.assertIn("must not be inside runtime result directory", str(ctx.exception))

    # ──────────────────────────────────────────────────────────────────────────
    # P1-5: 显式持久化目录失败 fail-closed 与路径一致性
    # ──────────────────────────────────────────────────────────────────────────

    def test_explicit_result_dir_permission_failure_fails_closed(self):
        """显式配置的 SEAGENT_RESULT_DIR 不可写或无法创建时必须报错，严禁静默 fallback 到仓库目录。"""
        # 模拟指向无写权限的路径（如以存在文件作为父目录的非法路径）
        blocker = Path(self.temp_dir) / "blocker_file"
        blocker.write_text("blocker", encoding="utf-8")
        uncreatable_dir = blocker / "illegal_subdir"

        os.environ["SEAGENT_RESULT_DIR"] = str(uncreatable_dir)

        # create=False 返回该显式路径
        self.assertEqual(get_result_dir(create=False), uncreatable_dir.resolve())

        # create=True 必须抛出异常 (NotADirectoryError / PermissionError / OSError)，不能静默 fallback
        with self.assertRaises((NotADirectoryError, PermissionError, OSError)):
            get_result_dir(create=True)

    def test_path_query_and_create_consistency(self):
        """正常配置下，create=False 与 create=True 返回的路径必须完全一致。"""
        valid_dir = Path(self.temp_dir) / "valid_storage"
        os.environ["SEAGENT_RESULT_DIR"] = str(valid_dir)

        path_query = get_result_dir(create=False)
        path_create = get_result_dir(create=True)

        self.assertEqual(path_query, path_create)
        self.assertTrue(path_create.exists())

    # ──────────────────────────────────────────────────────────────────────────
    # P1-3: 启动端口安全与进程隔离边界
    # ──────────────────────────────────────────────────────────────────────────

    def test_check_port_available_fails_closed_when_occupied(self):
        """端口被其他进程占用时必须报错中止（Fail-closed），严禁自动 kill 占用进程。"""
        import socket
        from run import check_port_available

        # 占用一个随机可用本地端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_sock:
            occupied_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied_sock.bind(("127.0.0.1", 0))
            occupied_sock.listen(1)
            occupied_port = occupied_sock.getsockname()[1]

            # 再次检查该端口必须报错，且原有 socket 依旧有效未被杀
            with self.assertRaises(RuntimeError) as ctx:
                check_port_available(occupied_port, host="127.0.0.1")
            self.assertIn("已被占用，启动中止", str(ctx.exception))

    def test_check_port_available_succeeds_when_free(self):
        """未被占用的端口必须正常返回无异常。"""
        import socket
        from run import check_port_available

        # 获取临时端口并释放
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as temp_sock:
            temp_sock.bind(("127.0.0.1", 0))
            free_port = temp_sock.getsockname()[1]

        # 释放后检查应该无异常
        check_port_available(free_port, host="127.0.0.1")

