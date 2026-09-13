"""Live smoke test script for SEAgent Marine Current MCP Server."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add src to sys.path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from seagent_marine_current.client import discover, make_client, query_current
from seagent_marine_current.contracts import CurrentQuery


def parse_args():
    parser = argparse.ArgumentParser(description="Live smoke verification of Marine Current MCP Server.")
    parser.add_argument("--latitude", type=float, default=19.6, help="Target latitude (default: 19.6)")
    parser.add_argument("--longitude", type=float, default=113.0, help="Target longitude (default: 113.0)")
    parser.add_argument("--depth-m", type=float, default=130.0, help="Target depth in meters (default: 130.0)")
    parser.add_argument("--offset-hours", type=float, default=24.0, help="Hours from now to start query (default: 24.0)")
    parser.add_argument("--hours", type=float, default=72.0, help="Duration of forecast query in hours (default: 72.0)")
    parser.add_argument("--prompt-credentials", action="store_true", help="Prompt for Copernicus username/password")
    parser.add_argument("--use-synthetic", action="store_true", help="Force synthetic offline data mode")
    parser.add_argument("--output", type=str, default="current_forecast.json", help="Output file path (default: current_forecast.json)")
    return parser.parse_args()


async def main_async(args):
    env_overrides = {}
    if args.prompt-credentials if hasattr(args, "prompt-credentials") else args.prompt_credentials:
        user = input("Copernicus Marine Username: ").strip()
        pwd = getpass.getpass("Copernicus Marine Password: ").strip()
        env_overrides["COPERNICUSMARINE_SERVICE_USERNAME"] = user
        env_overrides["COPERNICUSMARINE_SERVICE_PASSWORD"] = pwd

    if args.use_synthetic:
        env_overrides["SEAGENT_CURRENT_ENV"] = "test"
        env_overrides["SEAGENT_CURRENT_USE_SYNTHETIC"] = "1"

    now_utc = datetime.now(timezone.utc)
    start_time = now_utc + timedelta(hours=args.offset_hours)
    end_time = start_time + timedelta(hours=args.hours)

    query = CurrentQuery(
        latitude=args.latitude,
        longitude=args.longitude,
        operation_depth_m=args.depth_m,
        start_time=start_time,
        end_time=end_time,
    )

    print(f"[*] Querying Ocean Current Forecast:")
    print(f"    - Target Position: ({query.latitude:.4f}, {query.longitude:.4f})")
    print(f"    - Target Depth: {query.operation_depth_m:.1f} m")
    print(f"    - Target Interval: {query.start_time.isoformat()} to {query.end_time.isoformat()} ({args.hours:.1f}h)")
    print(f"    - Fingerprint: {query.fingerprint}")

    server_script = str(SRC_DIR / "seagent_marine_current" / "server.py")
    async with make_client(server_python=sys.executable, server_script=server_script, env=env_overrides) as client:
        print("[*] Performing MCP tools discovery...")
        tool_info = await discover(client)
        print(f"[+] Discovered tool: {tool_info['name']}")

        print("[*] Executing get_current_forecast tool call...")
        reply = await query_current(client, query)

        print(f"[*] Result status: {reply.status}")
        if reply.status == "OK" and reply.data:
            d = reply.data
            import math
            print("=" * 60)
            print("         OCEAN CURRENT FORECAST VERIFICATION REPORT")
            print("=" * 60)
            print(f"Provider:    {d.provider} (is_synthetic={d.is_synthetic})")
            print(f"Product ID:  {d.product}")
            print(f"Dataset ID:  {d.dataset}")
            print("-" * 60)
            print("REQUESTED:")
            print(f"  Latitude:  {query.latitude:.4f}")
            print(f"  Longitude: {query.longitude:.4f}")
            print(f"  Depth:     {query.operation_depth_m:.1f} m")
            print(f"  Interval:  {query.start_time.isoformat()} -> {query.end_time.isoformat()}")
            print("-" * 60)
            print("RESOLVED GRID & COORDINATES:")
            print(f"  Actual Latitude:  {d.actual_grid_latitude:.4f}")
            print(f"  Actual Longitude: {d.actual_grid_longitude:.4f}")
            print(f"  Distance:         {d.grid_distance_km:.2f} km")
            print("-" * 60)
            print("NATIVE DEPTH LEVELS:")
            if len(d.native_depth_layers_m) == 2:
                lower, upper = d.native_depth_layers_m[0], d.native_depth_layers_m[1]
                print(f"  Lower Level: {lower} m")
                print(f"  Upper Level: {upper} m")
                is_bounded = (lower <= query.operation_depth_m <= upper)
                print(f"  Clamped Bounding Check: {lower} <= {query.operation_depth_m} <= {upper} -> {'PASSED' if is_bounded else 'FAILED'}")
            else:
                print(f"  Exact Level: {d.native_depth_layers_m[0]} m (Exact Match)")
            print("-" * 60)
            print("NATIVE TIMESTAMPS:")
            print(f"  Total Steps:  {len(d.native_time_steps)}")
            print(f"  First Node:   {d.native_time_steps[0].isoformat()}")
            print(f"  Second Node:  {d.native_time_steps[1].isoformat() if len(d.native_time_steps) > 1 else 'N/A'}")
            print(f"  Last Node:    {d.native_time_steps[-1].isoformat()}")
            if len(d.native_time_steps) > 1:
                cadence = (d.native_time_steps[1] - d.native_time_steps[0]).total_seconds()
                print(f"  Cadence:      {cadence:.0f}s ({cadence/3600:.1f} hours)")
            print("-" * 60)
            # Count finite and NaN
            flat_uo = [x for row in d.uo for x in row]
            flat_vo = [x for row in d.vo for x in row]
            nan_count = sum(1 for x in flat_uo + flat_vo if math.isnan(x))
            finite_count = sum(1 for x in flat_uo + flat_vo if math.isfinite(x))
            print("MATRICES & SANITY CHECK:")
            print(f"  uo Shape:     [{len(d.uo)} x {len(d.uo[0])}]")
            print(f"  vo Shape:     [{len(d.vo)} x {len(d.vo[0])}]")
            print(f"  Units:        m/s")
            print(f"  Finite Count: {finite_count}")
            print(f"  NaN Count:    {nan_count}")
            print(f"  Retrieved At: {d.retrieved_at.isoformat()}")
            print("-" * 60)
            print("FIRST FEW NATIVE NODES SPEED SANITY CHECK (V = sqrt(u^2 + v^2)):")
            print(f"  {'Timestamp':<25} | {'Depth(m)':<8} | {'u (m/s)':<8} | {'v (m/s)':<8} | {'V (m/s)':<8}")
            print("  " + "-" * 65)
            for idx in range(min(4, len(d.native_time_steps))):
                t_iso = d.native_time_steps[idx].strftime("%Y-%m-%d %H:%M:%S")
                for d_idx, z_val in enumerate(d.native_depth_layers_m):
                    u_val = d.uo[idx][d_idx]
                    v_val = d.vo[idx][d_idx]
                    spd = math.hypot(u_val, v_val)
                    print(f"  {t_iso:<25} | {z_val:<8.1f} | {u_val:<8.4f} | {v_val:<8.4f} | {spd:<8.4f}")
            print("=" * 60)
            out_path = Path(args.output)
            out_path.write_text(reply.model_dump_json(indent=2), encoding="utf-8")
            print(f"[+] Saved complete forecast payload to {out_path.resolve()}")
            return 0
        else:
            err = reply.error
            print(f"[-] Evaluation failed with code: {err.code if err else 'UNKNOWN'}")
            print(f"    Message: {err.message if err else ''}")
            return 2


def main():
    args = parse_args()
    ret = asyncio.run(main_async(args))
    sys.exit(ret)


if __name__ == "__main__":
    main()
