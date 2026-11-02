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
    parser.add_argument("--input", "--file", dest="file", type=str, default="current_forecast.json", help="Path to forecast JSON (default: current_forecast.json)")
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

    print("=" * 60)
    print("      OPERATION WINDOW ALGORITHM PENETRATION VERIFICATION")
    print("=" * 60)
    print(f"Data Provenance:  {data.provider} (is_synthetic={data.is_synthetic})")
    print(f"Dataset ID:       {data.dataset}")
    print(f"Resolved Grid:    ({data.actual_grid_latitude:.4f}, {data.actual_grid_longitude:.4f})")
    print(f"Native Depths:    {data.native_depth_layers_m} m")
    print(f"Native Interval:  {t_start.isoformat()} -> {t_end.isoformat()} ({len(data.native_time_steps)} steps)")
    print(f"Operation Depth:  {args.depth_m:.1f} m")
    print(f"Current Limit:    {args.current_limit:.2f} m/s")
    print("-" * 60)

    # 1. Run CHECK on full interval
    print("1. OPERATION WINDOW CHECK (Fixed Interval Algorithm):")
    check_res = OperationWindowService.check_window(
        forecast=data,
        operation_depth_m=args.depth_m,
        start_time=t_start,
        end_time=t_end,
        current_limit_mps=args.current_limit,
    )
    print(f"  Result Status:  {check_res.status}")
    print(f"  Reason Code:    {check_res.reason_code or 'NONE'}")
    if check_res.v_max_mps is not None:
        print(f"  Max Current:    {check_res.v_max_mps:.4f} m/s (Limit: {check_res.current_limit_mps:.2f} m/s)")
    print(f"  Basis:          {check_res.basis}")
    print(f"  Execution Auth: {check_res.execution_authorized}")
    print("-" * 60)

    # 2. Run SEARCH for task windows
    print(f"2. OPERATION WINDOW SEARCH (Dynamic Windows, duration={args.duration_hours:.1f}h):")
    search_res = OperationWindowService.search_windows(
        forecast=data,
        operation_depth_m=args.depth_m,
        search_start=t_start,
        search_end=t_end,
        duration_hours=args.duration_hours,
        current_limit_mps=args.current_limit,
        candidate_step_seconds=3600.0,
    )
    print(f"  Result Status:  {search_res.status}")
    print(f"  Candidates:     {len(search_res.available_windows)} suitable window(s) found")
    for idx, win in enumerate(search_res.available_windows[:5], 1):
        st_str = win.start_time.strftime("%Y-%m-%d %H:%M")
        et_str = win.end_time.strftime("%Y-%m-%d %H:%M")
        print(f"  [{idx}] {st_str} -> {et_str} | V_max = {win.v_max_mps:.4f} m/s | Margin = {win.safety_margin:+.4f} m/s")
    print("=" * 60)


if __name__ == "__main__":
    main()
