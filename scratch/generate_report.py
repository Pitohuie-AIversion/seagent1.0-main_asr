"""
scratch/generate_report.py — Backward compatibility facade.
Delegates to scripts/generate_report.py.
"""

from pathlib import Path
from scripts.generate_report import (
    load_replacement_map,
    classify_audit_item,
    generate_report,
)

__all__ = [
    "Path",
    "load_replacement_map",
    "classify_audit_item",
    "generate_report",
]

if __name__ == "__main__":
    import sys
    from scripts.generate_report import main
    main()
