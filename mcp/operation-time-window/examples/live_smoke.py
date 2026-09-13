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
            print(f"[+] Provenance: provider={d.provider}, dataset={d.dataset}, is_synthetic={d.is_synthetic}")
            print(f"[+] Nearest Grid Point: ({d.actual_grid_latitude:.4f}, {d.actual_grid_longitude:.4f}) - Distance: {d.grid_distance_km:.2f} km")
            print(f"[+] Target Depth {query.operation_depth_m}m mapped to native bounding layers: {d.native_depth_layers_m} m")
            print(f"[+] Native Time Nodes ({len(d.native_time_steps)} steps, ~6h cadence):")
            print(f"    From: {d.native_time_steps[0].isoformat()} To: {d.native_time_steps[-1].isoformat()}")
            print(f"[+] Current Matrices: uo[{len(d.uo)}][{len(d.uo[0])}], vo[{len(d.vo)}][{len(d.vo[0])}] in m/s")
            print(f"    First step uo/vo: u={d.uo[0]}, v={d.vo[0]}")
            print(f"    Last step uo/vo:  u={d.uo[-1]}, v={d.vo[-1]}")
            print(f"[+] System Retrieved At: {d.retrieved_at.isoformat()}, Model Run: {d.model_run}")
            out_path = Path(args.output)
            out_path.write_text(reply.model_dump_json(indent=2), encoding="utf-8")
            print(f"[+] Saved forecast payload to {out_path.resolve()}")
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
