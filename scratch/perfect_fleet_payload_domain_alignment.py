import yaml

target = {
    "crawler_heavy_seabed_robot_1600hp": {
        "onboard": ["高压水射流喷冲埋设模块", "海缆压埋装置", "埋深控制装置", "高清水下摄像机", "云台摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "USBL定位设备"],
        "supported": ["机械切割开沟模块", "海缆保护施工工具", "TSS管缆跟踪传感器", "激光标尺", "多波束声呐", "机械式声呐", "水下定位信标", "海床地质探测设备", "管缆外观检测设备", "腐蚀检测探头", "CTD"],
    },
    "towed_heavy_seabed_robot_1500hp": {
        "onboard": ["高压水射流喷冲埋设模块", "海缆压埋装置", "埋深控制装置", "高清水下摄像机", "云台摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "USBL定位设备"],
        "supported": ["机械切割开沟模块", "激光标尺", "多波束声呐", "机械式声呐", "水下定位信标", "海床地质探测设备", "管缆外观检测设备", "腐蚀检测探头", "CTD"],
    },
    "special_work_class_robot_600hp": {
        "onboard": ["高压水射流喷冲埋设模块", "海缆压埋装置", "埋深控制装置", "高清水下摄像机", "云台摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "多波束声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "履带与滑橇组件", "多功能液压机械臂", "USBL定位设备"],
        "supported": ["机械切割开沟模块", "TSS管缆跟踪传感器", "激光标尺", "机械式声呐", "水下定位信标", "海床地质探测设备", "管缆外观检测设备", "腐蚀检测探头", "CTD"],
    },
    "general_work_class_rov_250hp": {
        "onboard": ["高清水下摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "多波束声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "多功能液压机械臂", "电液机械臂", "夹爪", "USBL定位设备"],
        "supported": ["双目视觉模块", "激光标尺", "三维视觉系统", "机械式声呐", "清洗刷", "采样器", "液压剪切器", "CTD", "泄漏检测传感器", "水质传感器"],
    },
    "light_work_class_rov_150hp": {
        "onboard": ["高清水下摄像机", "云台摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "多波束声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "电液机械臂", "多功能液压机械臂", "USBL定位设备"],
        "supported": ["激光标尺", "双目视觉模块", "机械式声呐", "TSS管缆跟踪传感器", "腐蚀检测探头", "厚度检测传感器", "CTD", "泄漏检测传感器", "水质传感器", "采样器"],
    },
    "observation_rov_75hp": {
        "onboard": ["高清水下摄像机", "云台摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "多波束声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器", "USBL定位设备"],
        "supported": ["激光标尺", "双目视觉模块", "机械式声呐", "TSS管缆跟踪传感器", "腐蚀检测探头", "厚度检测传感器", "CTD", "泄漏检测传感器", "水质传感器", "采样器"],
    },
    "autonomous_underwater_vehicle_324cc": {
        "onboard": ["前视声呐", "成像声呐", "多波束声呐", "高清水下摄像机", "LED水下照明灯", "INS惯性导航系统", "DVL多普勒测速仪", "USBL定位设备", "深度传感器"],
        "supported": ["侧扫声呐", "合成孔径声呐", "浅地层剖面仪", "光学测量模块", "激光标尺", "CTD", "水质传感器"],
    },
}

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

for variant_id, spec in target.items():
    if variant_id in data.get("model_variants", {}):
        hp = data["model_variants"][variant_id]["hard_params"]
        hp["onboard_payloads"] = spec["onboard"]
        hp["supported_payloads"] = spec["supported"]

# Verify no overlap between onboard and supported for any variant
for variant_id, variant in data.get("model_variants", {}).items():
    hp = variant.get("hard_params", {})
    onboard = set(hp.get("onboard_payloads", []))
    supported = set(hp.get("supported_payloads", []))
    overlap = onboard.intersection(supported)
    assert not overlap, f"Overlap in {variant_id}: {overlap}"

print("Perfect domain alignment verified with ZERO overlap!")
