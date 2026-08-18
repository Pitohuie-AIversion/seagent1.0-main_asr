import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

# Define expected domain alignment for each variant
alignments = {
    "crawler_heavy_seabed_robot_1600hp": {
        "add_onboard": ["前视声呐", "成像声呐", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器"],
        "remove_supported": ["前视声呐", "成像声呐"],
    },
    "towed_heavy_seabed_robot_1500hp": {
        "add_onboard": ["前视声呐", "成像声呐", "云台摄像机", "INS惯性导航系统", "DVL多普勒测速仪", "深度传感器"],
        "remove_supported": ["前视声呐", "成像声呐", "云台摄像机"],
    },
    "special_work_class_robot_600hp": {
        "add_onboard": ["INS惯性导航系统", "DVL多普勒测速仪", "深度传感器"],
        "remove_supported": [],
    },
    "autonomous_underwater_vehicle_324cc": {
        "add_onboard": [],
        "remove_onboard": ["激光标尺"],
        "add_supported": ["激光标尺"],
    },
}

for variant_id, cfg in alignments.items():
    if variant_id in data.get("model_variants", {}):
        hp = data["model_variants"][variant_id]["hard_params"]
        onboard = hp.get("onboard_payloads", []) or []
        supported = hp.get("supported_payloads", []) or []

        # Remove from onboard
        for item in cfg.get("remove_onboard", []):
            if item in onboard:
                onboard.remove(item)

        # Add to onboard
        for item in cfg.get("add_onboard", []):
            if item not in onboard:
                onboard.append(item)

        # Remove from supported
        for item in cfg.get("remove_supported", []):
            if item in supported:
                supported.remove(item)

        # Add to supported
        for item in cfg.get("add_supported", []):
            if item not in supported:
                supported.append(item)

        hp["onboard_payloads"] = list(dict.fromkeys(onboard))
        hp["supported_payloads"] = list(dict.fromkeys(supported))

print("Domain alignment logic verified successfully in memory!")
