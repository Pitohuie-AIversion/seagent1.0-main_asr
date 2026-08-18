import yaml

fleet = yaml.safe_load(open("config/robot_fleet.yaml"))
assets = yaml.safe_load(open("config/assets.yaml"))

fleet_payloads = set()
for model_key, model_cfg in fleet.get("model_variants", {}).items():
    hp = model_cfg.get("hard_params", {})
    for p in (hp.get("onboard_payloads", []) or []) + (hp.get("supported_payloads", []) or []):
        fleet_payloads.add(p.strip())

catalog_primary_names = set()
catalog_map = {}
for key, item in assets.get("payload_catalog", {}).items():
    if isinstance(item, dict):
        name = item.get("name")
        if name:
            catalog_primary_names.add(name.strip())
            catalog_map[name.strip()] = key

print("=== Fleet Payloads ===")
for p in sorted(fleet_payloads):
    print(" -", p)

print("\n=== Catalog Primary Names ===")
for p in sorted(catalog_primary_names):
    print(" -", p)
