
## 2024-05-18 - [YAML Configuration Loading Bottleneck]
**Learning:** `yaml.safe_load` on large configuration files like `robot_fleet.yaml` can be a significant bottleneck (taking ~60-90ms) when called multiple times during `KnowledgeBase` initializations.
**Action:** Use `@functools.lru_cache(maxsize=None)` on functions that repeatedly parse static YAML files to avoid redundant I/O and parsing overhead.
