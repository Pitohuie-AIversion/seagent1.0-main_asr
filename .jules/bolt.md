
## 2024-05-24 - Cache static YAML configuration parsing
**Learning:** This codebase frequently parses static YAML configurations (e.g., `robot_fleet.yaml`, `assets.yaml`, `task_schemas.yaml`) during `KnowledgeBase` instantiation, or state resolution (`load_fleet`), causing significant redundant `yaml.safe_load` overhead, impacting initialization time by orders of magnitude (e.g. 26s vs 3.5s in tight loops).
**Action:** When working with configuration loading patterns that rely on `yaml.safe_load` for static config files, applying `@functools.lru_cache` to the loading functions prevents severe redundant parsing overhead and makes repeated calls extremely fast.

## 2024-05-24 - Cache static YAML configuration parsing in EnvironmentInfo
**Learning:** Instantiating `EnvironmentInfo` (which is often done during initialization of `KnowledgeBase`) triggers `yaml.safe_load(f)` on `oilfield.yaml`. Profiling showed this operation to take ~27ms per call without caching, which quickly adds up. Like other static YAML loading optimizations in the codebase, this is a prime candidate for `@functools.lru_cache` based on file `mtime_ns`. We discovered that `EnvironmentInfo.__init__` was performing heavy I/O and CPU work redundantly.
**Action:** Always wrap static YAML parsing inside a cached function utilizing `mtime_ns` for cache invalidation, and apply `copy.deepcopy()` to the returned result when used within constructors to prevent instances from mutating the shared cache structure.
