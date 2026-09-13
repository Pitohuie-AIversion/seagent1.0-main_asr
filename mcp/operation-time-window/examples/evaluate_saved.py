"""Evaluate operation windows on saved forecast JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to sys.path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from seagent_marine_current.contracts import ForecastReply
from seagent_marine_current.windows import OperationWindowService


def parse_args():
    parser = argparse.ArgumentParser(description="Evaluate operation windows on saved forecast.")
    parser.add_argument("--file", type=str, default="current_forecast.json", help="Path to forecast JSON")
    parser.add_argument("--depth-m", type=float, default=130.0, help="Target operation depth (m)")
    parser.add_argument("--current-limit", type=float, default=0.5, help="Current speed limit in m/s (default: 0.5)")
    parser.add_argument("--duration-hours", type=float, default=4.0, help="Task duration in hours for SEARCH (default: 4.0)")
    return parser.parse_args()


def main():
    args = parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"Error: file {path} not found.")
        sys.exit(1)

    reply_dict = json.loads(path.read_text(encoding="utf-8"))
    reply = ForecastReply.model_validate(reply_dict)

    if reply.status != "OK" or reply.data is None:
        print(f"Error: Saved forecast does not contain valid data (status={reply.status})")
        sys.exit(1)

    data = reply.data
    t_start = data.native_time_steps[0]
    t_end = data.native_time_steps[-1]

    print(f"[*] Loaded forecast: {len(data.native_time_steps)} native steps from {t_start.isoformat()} to {t_end.isoformat()}")

    # 1. Run CHECK on full interval
    print("\n--- Running Window CHECK ---")
    check_res = OperationWindowService.check_window(
        forecast=data,
        operation_depth_m=args.depth_m,
        start_time=t_start,
        end_time=t_end,
        current_limit_mps=args.current_limit,
    )
    print(f"CHECK Result: {check_res.status}")
    print(f"  Message: {check_res.message}")
    if check_res.v_max_mps is not None:
        print(f"  V_max: {check_res.v_max_mps:.3f} m/s (Limit: {check_res.current_limit_mps:.2f} m/s)")

    # 2. Run SEARCH for task windows
    print(f"\n--- Running Window SEARCH (duration={args.duration_hours:.1f}h) ---")
    search_res = OperationWindowService.search_windows(
        forecast=data,
        operation_depth_m=args.depth_m,
        search_start=t_start,
        search_end=t_end,
        duration_hours=args.duration_hours,
        current_limit_mps=args.current_limit,
        candidate_step_seconds=3600.0,
    )
    print(f"SEARCH Result: {search_res.status} ({len(search_res.available_windows)} suitable windows found)")
    print(f"  Message: {search_res.message}")
    for idx, win in enumerate(search_res.available_windows[:5], 1):
        print(f"  [{idx}] {win.start_time.strftime('%Y-%m-%d %H:%M')} to {win.end_time.strftime('%Y-%m-%d %H:%M')} | Vmax = {win.v_max_mps:.3f} m/s")


if __name__ == "__main__":
    main()
