import yaml

with open('/root/mzy/seagent1.0-main_asr/config/assets.yaml', 'r', encoding='utf-8') as f:
    assets = yaml.safe_load(f)

catalog = assets.get('payload_catalog', {})
print(f'Total catalog entries in assets.yaml: {len(catalog)}')
for k, v in catalog.items():
    print(f'{k}: name="{v.get("name")}", aliases={v.get("aliases")}, capabilities={v.get("capabilities")}')
