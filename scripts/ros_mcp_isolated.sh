#!/bin/sh
set -eu

cd /opt/ros-mcp-server
export PYTHONNOUSERSITE=1
export PYTHONPATH=/opt/ros-mcp-server/site-packages
exec python3 -m ros_mcp.main "$@"
