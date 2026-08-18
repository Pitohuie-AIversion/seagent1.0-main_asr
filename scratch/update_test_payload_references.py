from pathlib import Path

test_files = [
    "tests/test_visible_ordinal_selection.py",
    "tests/test_pre_publish_environment_and_robot_state_validation.py",
    "tests/test_robot_capability_preselection.py",
    "tests/test_normalization_runtime_v2.py",
    "tests/test_issue_12_slot_cascade.py",
    "tests/test_issue_14_validator_snapshot.py",
]

for filepath in test_files:
    p = Path(filepath)
    if p.exists():
        content = p.read_text(encoding="utf-8")
        if "电磁检测传感器" in content:
            new_content = content.replace("电磁检测传感器", "TSS管缆跟踪传感器")
            p.write_text(new_content, encoding="utf-8")
            print(f"Updated {filepath}")
