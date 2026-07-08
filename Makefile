# ==============================================================================
# Makefile - 水下多智能体任务决策大模型项目自动化命令工具
# ==============================================================================

PYTHON = /root/miniconda3/envs/seagent/bin/python

.PHONY: help test run clean status

help:
	@echo "可用命令列表:"
	@echo "  make run    - 启动 Flask 后端服务器 (运行 run.py)"
	@echo "  make test   - 在 seagent 环境下执行完整单元测试套件"
	@echo "  make clean  - 清理 __pycache__ 及临时文件"
	@echo "  make status - 查看后端 Flask 运行状态 (8890 端口)"

run:
	@echo "🚀 正在启动服务器..."
	$(PYTHON) run.py

test:
	@echo "🧪 正在执行单元测试..."
	$(PYTHON) -m unittest discover tests

clean:
	@echo "🧹 正在清理缓存文件..."
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +

status:
	@echo "🔍 检查端口 8890 监听状态:"
	fuser -n tcp 8890 || echo "端口 8890 当前没有被监听 (服务可能未启动)。"
