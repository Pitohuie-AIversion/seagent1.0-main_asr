
## 2024-05-24 - Cache static YAML configuration parsing
**Learning:** This codebase frequently parses static YAML configurations (e.g., `robot_fleet.yaml`, `assets.yaml`, `task_schemas.yaml`) during `KnowledgeBase` instantiation, or state resolution (`load_fleet`), causing significant redundant `yaml.safe_load` overhead, impacting initialization time by orders of magnitude (e.g. 26s vs 3.5s in tight loops).
**Action:** When working with configuration loading patterns that rely on `yaml.safe_load` for static config files, applying `@functools.lru_cache` to the loading functions prevents severe redundant parsing overhead and makes repeated calls extremely fast.
