import subprocess
import time
import os

SEAGENT_ROOT = "/root/mzy/seagent1.0-main_asr"
ROS_ENV = "source /opt/ros/humble/setup.bash && source /root/ros2_ws/install/setup.bash && ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=9090"

# 1. Start rosbridge 9090 with clean system python 3.10 env
print("Starting rosbridge on 9090...")
p_ros = subprocess.Popen(
    "env -i HOME=$HOME PATH=/opt/ros/humble/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -c 'source /opt/ros/humble/setup.bash && source /root/ros2_ws/install/setup.bash && ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=9090'",
    shell=True,
    cwd=SEAGENT_ROOT,
    start_new_session=True,
    stdout=open("/tmp/rosbridge.log", "w"),
    stderr=subprocess.STDOUT
)
time.sleep(2)

# 2. Start backend run.py --mcp on 6006
print("Starting SEAgent backend on 6006...")
backend_env = os.environ.copy()
backend_env.update({
    "PORT": "6006",
    "OFFLINE_MOCK": "1",
    "MCP_EMBEDDED_MOCK": "0",
    "MCP_HOST": "127.0.0.1",
    "MCP_PORT": "9090",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
})
p_backend = subprocess.Popen(
    ["/root/miniconda3/envs/seagent/bin/python", "run.py", "--mcp"],
    cwd=SEAGENT_ROOT,
    env=backend_env,
    start_new_session=True,
    stdout=open("/tmp/backend.log", "w"),
    stderr=subprocess.STDOUT
)
time.sleep(2)

# 3. Start telemetry echo node
print("Starting telemetry echo node...")
p_echo = subprocess.Popen(
    ["/root/miniconda3/envs/seagent/bin/python", "scratch/run_ros2_telemetry_echo_node.py"],
    cwd=SEAGENT_ROOT,
    start_new_session=True,
    stdout=open("/tmp/echo_node.log", "w"),
    stderr=subprocess.STDOUT
)
time.sleep(1)

# 4. Start dashboard server on 8088
print("Starting dashboard server on 8088...")
p_dash = subprocess.Popen(
    ["/root/miniconda3/envs/seagent/bin/python", "dashboard_server.py", "8088"],
    cwd=SEAGENT_ROOT,
    start_new_session=True,
    stdout=open("/tmp/dashboard.log", "w"),
    stderr=subprocess.STDOUT
)
time.sleep(1)

print("All 4 daemons started successfully in correct order.")
