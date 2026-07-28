# SEAgent GitHub Actions CI Dependency Installation Failure Record

## Overview
- **Run ID**: `30256398910`
- **Job ID**: `89945911141` (`test-and-verify`)
- **Step**: `Install Dependencies`
- **OS**: `ubuntu-latest` (Ubuntu Linux 6.8.0-1021-azure)
- **Python Version**: `3.10`
- **Failing Command**: `pip install -r requirements.txt`

## Failing Package & Error Details

- **Failing Package**: `nvidia-cutlass-dsl-libs-base==4.5.0.dev0` and `flashinfer-python==0.6.6`
- **Error Type**: `pip._vendor.resolvelib.resolvers.ResolutionImpossible` / `ERROR: Could not find a version that satisfies the requirement`
- **Error Message**:
```text
ERROR: Could not find a version that satisfies the requirement nvidia-cutlass-dsl-libs-base==4.5.0.dev0 (from versions: none)
ERROR: No matching distribution found for nvidia-cutlass-dsl-libs-base==4.5.0.dev0
```

## Dependency Resolver Output Analysis

The root `requirements.txt` was generated as a full environment freeze from an AutoDL GPU server container. It includes specialized CUDA pre-release packages (`.dev0` builds), proprietary GPU acceleration extensions (`flashinfer-python`, `nvidia-cutlass-dsl-libs-base`), and specific hardware bindings (`cuda-bindings`, `nvidia-cublas-cu12`). 

When `pip install -r requirements.txt` ran in standard GitHub Actions `ubuntu-latest` CPU runners without custom PyPI index URLs or CUDA drivers, `pip` failed to resolve these pre-release packages on public PyPI, aborting the `Install Dependencies` step.

## Resolution
To decouple CI testing from GPU runtime dependencies:
1. Created `requirements/base.txt` with minimal CPU dependencies (`PyYAML`, `Flask`, `Werkzeug`).
2. Created `requirements/test.txt` (`-r base.txt`).
3. Created `requirements/gpu.txt` for optional GPU/ASR packages.
4. Preserved original `requirements.txt` as a full container snapshot.
5. Configured GitHub Actions to install lightweight `requirements/test.txt`.
