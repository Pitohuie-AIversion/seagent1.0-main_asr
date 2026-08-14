"""测试包初始化：包方式执行 unittest 时同样启用产物隔离。"""

from .runtime_isolation import configure_test_artifact_paths


configure_test_artifact_paths()
