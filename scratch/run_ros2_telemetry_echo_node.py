"""
scratch/run_ros2_telemetry_echo_node.py — Backward compatibility facade.
Delegates dynamically to scripts/run_ros2_telemetry_echo_node.py.
"""

from scripts import run_ros2_telemetry_echo_node as _impl

# Expose all symbols (including internal/private ones) for backward compatibility
for _k, _v in _impl.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

if __name__ == "__main__":
    _impl.main()
