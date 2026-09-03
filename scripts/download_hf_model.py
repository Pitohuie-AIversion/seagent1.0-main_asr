#!/usr/bin/env python3
"""Resumable Hugging Face mirror downloader for large Xet-backed files."""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import socket
from pathlib import Path

import requests


def _fixed_dns(host: str, addresses: list[str]):
    original = socket.getaddrinfo

    def resolve(name, *args, **kwargs):
        if name == host:
            return [item for address in addresses for item in original(address, *args, **kwargs)]
        return original(name, *args, **kwargs)

    socket.getaddrinfo = resolve


def download(repo: str, filename: str, output: Path, workers: int, chunk_size: int) -> None:
    mirror = os.environ.get("HF_MIRROR", "https://hf-mirror.com").rstrip("/")
    mirror_host = mirror.split("/", 3)[2]
    _fixed_dns(mirror_host, os.environ.get("HF_MIRROR_IPS", "160.16.86.14").split(","))
    resolver = f"{mirror}/{repo}/resolve/main/{filename}?download=true"
    session = requests.Session()
    response = session.get(resolver, allow_redirects=False, timeout=60)
    response.raise_for_status()
    target = response.headers.get("location")
    if not target:
        raise RuntimeError(f"mirror did not return a CDN location for {filename}")
    cdn_host = target.split("/", 3)[2]
    _fixed_dns(cdn_host, os.environ.get("HF_CDN_IPS", "47.131.129.243,13.215.180.201").split(","))
    probe = session.get(target, headers={"Range": "bytes=0-0"}, timeout=60, verify=False)
    probe.raise_for_status()
    total = int(probe.headers["content-range"].rsplit("/", 1)[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_suffix(output.suffix + ".part")
    done_dir = output.with_suffix(output.suffix + ".chunks")
    done_dir.mkdir(parents=True, exist_ok=True)
    with part.open("ab") as f:
        f.truncate(total)

    ranges = [(start, min(start + chunk_size, total) - 1) for start in range(0, total, chunk_size)]

    def fetch(item: tuple[int, int]) -> int:
        start, end = item
        marker = done_dir / f"{start}-{end}"
        if marker.exists():
            return end - start + 1
        for attempt in range(8):
            try:
                r = session.get(target, headers={"Range": f"bytes={start}-{end}"}, timeout=120, verify=False)
                r.raise_for_status()
                data = r.content
                if len(data) != end - start + 1:
                    raise RuntimeError(f"short range: {len(data)} != {end - start + 1}")
                with part.open("r+b") as f:
                    f.seek(start)
                    f.write(data)
                marker.touch()
                return len(data)
            except Exception:
                if attempt == 7:
                    raise
        return 0

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for size in pool.map(fetch, ranges):
            done += size
            print(f"{filename}: {done / total:.1%}", flush=True)
    part.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("filename")
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-mb", type=int, default=16)
    args = parser.parse_args()
    download(args.repo, args.filename, args.output, args.workers, args.chunk_mb * 1024 * 1024)


if __name__ == "__main__":
    main()
