import yaml
from pathlib import Path

fleet_path = Path("config/robot_fleet.yaml")
assets_path = Path("config/assets.yaml")

fleet_data = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
assets_data = yaml.safe_load(assets_path.read_text(encoding="utf-8"))

fleet_payloads = set()
for model_key, model_cfg in fleet_data.get("model_variants", {}).items():
    hp = model_cfg.get("hard_params", {})
    for p in hp.get("onboard_payloads", []) or []:
        fleet_payloads.add(p.strip())
    for p in hp.get("supported_payloads", []) or []:
        fleet_payloads.add(p.strip())

catalog_names = set()
catalog_aliases = set()
for cat_key, cat_cfg in assets_data.get("payload_catalog", {}).items():
    if isinstance(cat_cfg, dict):
        name = cat_cfg.get("name")
        if name:
            catalog_names.add(name.strip())
        for a in cat_cfg.get("aliases", []) or []:
            catalog_aliases.add(a.strip())

option_payloads = set()
for task_key, task_cfg in assets_data.get("payload_options", {}).items():
    if isinstance(task_cfg, dict):
        for p in task_cfg.get("common", []) or []:
            option_payloads.add(p.strip())

print("=== Fleet Payloads Count ===", len(fleet_payloads))
print("=== Catalog Names Count ===", len(catalog_names))
print("=== Task Option Payloads Count ===", len(option_payloads))

print("\n--- Payloads in Fleet BUT NOT as primary name in Catalog ---")
not_in_catalog_name = fleet_payloads - catalog_names
for p in sorted(not_in_catalog_name):
    in_alias = p in catalog_aliases
    print(f"  - '{p}' (In catalog aliases: {in_alias})")

print("\n--- Payloads in Task Options BUT NOT as primary name in Catalog ---")
opt_not_in_catalog = option_payloads - catalog_names
for p in sorted(opt_not_in_catalog):
    in_alias = p in catalog_aliases
    print(f"  - '{p}' (In catalog aliases: {in_alias})")
