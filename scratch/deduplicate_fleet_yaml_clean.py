from pathlib import Path

fleet_path = Path("config/robot_fleet.yaml")
lines = fleet_path.read_text(encoding="utf-8").splitlines()

new_lines = []
current_section = None
seen_items = set()

for line in lines:
    stripped = line.strip()
    if stripped.startswith("onboard_payloads:") or stripped.startswith("supported_payloads:"):
        current_section = stripped.split(":")[0]
        seen_items = set()
        new_lines.append(line)
        continue
    elif stripped.endswith(":") and not stripped.startswith("-"):
        current_section = None
        seen_items = set()
        new_lines.append(line)
        continue

    if current_section and stripped.startswith("- "):
        item_val = stripped[2:].strip().strip('"').strip("'")
        if item_val in seen_items:
            # Duplicate item, skip line
            continue
        seen_items.add(item_val)
        new_lines.append(line)
    else:
        new_lines.append(line)

fleet_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
print("Cleanly deduplicated payload lines in robot_fleet.yaml while preserving all comments!")
