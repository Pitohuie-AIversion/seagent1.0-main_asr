#!/usr/bin/env python3
"""Validate a portable case catalog and optionally compare source case IDs."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys


ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d{2,}$")
HEADING_PATTERN = re.compile(r"^###\s+([A-Z][A-Z0-9_]*-\d{2,})\b", re.MULTILINE)
REQUIRED_CASE_FIELDS = {
    "id",
    "title",
    "portable_invariant",
    "layers",
    "outcome",
    "oracle",
    "next_gen_status",
}
ALLOWED_STATUSES = {"planned", "adapted", "verified", "deferred"}
ALLOWED_ORACLES = {
    "artifact",
    "semantic-fallback",
    "state-transition",
    "structured",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--runner", type=Path)
    parser.add_argument("--manual-doc", type=Path)
    parser.add_argument("--case-class", default="IntegrationTestCase")
    parser.add_argument(
        "--require-status",
        choices=sorted(ALLOWED_STATUSES),
        help="Fail unless every catalog case has this migration status.",
    )
    return parser.parse_args()


def load_catalog(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read catalog {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("catalog root must be an object")
    return data


def validate_catalog(data: dict, required_status: str | None) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if not isinstance(data.get("schema_version"), int):
        errors.append("schema_version must be an integer")
    if not isinstance(data.get("suite"), str) or not data["suite"].strip():
        errors.append("suite must be a non-empty string")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return [], errors + ["cases must be a non-empty list"]

    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        location = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{location} must be an object")
            continue
        missing = REQUIRED_CASE_FIELDS - case.keys()
        if missing:
            errors.append(f"{location} missing fields: {sorted(missing)}")
            continue

        case_id = case["id"]
        if not isinstance(case_id, str) or not ID_PATTERN.fullmatch(case_id):
            errors.append(f"{location}.id is invalid: {case_id!r}")
        elif case_id in seen:
            errors.append(f"duplicate case id: {case_id}")
        else:
            seen.add(case_id)

        for field in ("title", "portable_invariant", "outcome"):
            if not isinstance(case[field], str) or not case[field].strip():
                errors.append(f"{location}.{field} must be a non-empty string")
        if not isinstance(case["layers"], list) or not case["layers"] or not all(
            isinstance(layer, str) and layer.strip() for layer in case["layers"]
        ):
            errors.append(f"{location}.layers must be a non-empty string list")
        if case["oracle"] not in ALLOWED_ORACLES:
            errors.append(f"{location}.oracle is invalid: {case['oracle']!r}")
        if case["next_gen_status"] not in ALLOWED_STATUSES:
            errors.append(
                f"{location}.next_gen_status is invalid: {case['next_gen_status']!r}"
            )
        if case["next_gen_status"] == "deferred" and not case.get("defer_reason"):
            errors.append(f"{location}.defer_reason is required for deferred cases")
        if required_status and case["next_gen_status"] != required_status:
            errors.append(
                f"{case_id} has status {case['next_gen_status']!r}, "
                f"expected {required_status!r}"
            )
    return cases, errors


def runner_case_ids(path: Path, case_class: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot parse runner {path}: {exc}") from exc
    ids: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        first = node.args[0]
        if name == case_class and isinstance(first, ast.Constant) and isinstance(first.value, str):
            ids.append(first.value)
    return ids


def manual_case_ids(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read manual document {path}: {exc}") from exc
    return HEADING_PATTERN.findall(text)


def compare_ids(label: str, observed: list[str], catalog_ids: list[str]) -> list[str]:
    errors: list[str] = []
    duplicates = sorted(case_id for case_id, count in Counter(observed).items() if count > 1)
    if duplicates:
        errors.append(f"{label} contains duplicate IDs: {duplicates}")
    missing = sorted(set(catalog_ids) - set(observed))
    extra = sorted(set(observed) - set(catalog_ids))
    if missing:
        errors.append(f"{label} is missing catalog IDs: {missing}")
    if extra:
        errors.append(f"{label} has IDs absent from catalog: {extra}")
    return errors


def print_summary(cases: list[dict]) -> None:
    layers = Counter(layer for case in cases for layer in case.get("layers", []))
    outcomes = Counter(case.get("outcome") for case in cases)
    oracles = Counter(case.get("oracle") for case in cases)
    statuses = Counter(case.get("next_gen_status") for case in cases)
    print(f"Catalog cases: {len(cases)}")
    print("Layers: " + ", ".join(f"{key}={value}" for key, value in sorted(layers.items())))
    print("Outcomes: " + ", ".join(f"{key}={value}" for key, value in sorted(outcomes.items())))
    print("Oracles: " + ", ".join(f"{key}={value}" for key, value in sorted(oracles.items())))
    print("Statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))


def main() -> int:
    args = parse_args()
    try:
        data = load_catalog(args.catalog)
        cases, errors = validate_catalog(data, args.require_status)
        catalog_ids = [
            case["id"] for case in cases if isinstance(case, dict) and "id" in case
        ]
        if args.runner:
            errors.extend(
                compare_ids("runner", runner_case_ids(args.runner, args.case_class), catalog_ids)
            )
        if args.manual_doc:
            errors.extend(
                compare_ids("manual document", manual_case_ids(args.manual_doc), catalog_ids)
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print_summary(cases)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Catalog audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
