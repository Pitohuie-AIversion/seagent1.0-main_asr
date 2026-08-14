"""pytest 全局引导：在收集测试模块前隔离所有运行产物。"""

from tests.runtime_isolation import configure_test_artifact_paths


TEST_RESULT_ROOT = configure_test_artifact_paths()
