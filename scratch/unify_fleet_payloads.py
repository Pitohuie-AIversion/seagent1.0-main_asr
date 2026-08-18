from pathlib import Path

fleet_path = Path("config/robot_fleet.yaml")
text = fleet_path.read_text(encoding="utf-8")

replacements = [
    ("电磁检测传感器", "TSS管缆跟踪传感器"),
    ("多功能机械臂", "多功能液压机械臂"),
    ("轻型电液机械臂", "电液机械臂"),
    ("切割工具", "液压剪切器"),
    ("检测探头", "腐蚀检测探头"),
    ("声学应答器", "水下定位信标"),
    ("履带组件", "履带与滑橇组件"),
    ("滑橇组件", "履带与滑橇组件"),
    ("浊度传感器", "水质传感器"),
    ("溶解氧传感器", "水质传感器"),
    ("温度传感器", "CTD"),
    ("压力传感器", "CTD"),
    ("高度计", "深度传感器"),
]

for old, new in replacements:
    text = text.replace(f'"{old}"', f'"{new}"')

fleet_path.write_text(text, encoding="utf-8")
print("Updated robot_fleet.yaml successfully!")
