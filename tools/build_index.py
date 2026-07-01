#!/usr/bin/env python3
"""Rebuild codes_index.json by scanning all codes/{platform}/*.json files.

Run from the repo root:
    python tools/build_index.py

Outputs:  custom_components/smartir/codes_index.json
"""
import json
import os
import sys

PLATFORMS = ["climate", "fan", "media_player", "light"]

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_TOOLS_DIR)
CODES_DIR = os.path.join(REPO_ROOT, "codes")
OUTPUT_FILE = os.path.join(
    REPO_ROOT, "custom_components", "smartir", "codes_index.json"
)


def build_index() -> dict:
    index: dict[str, list] = {}
    total = 0
    errors = 0

    for platform in PLATFORMS:
        platform_dir = os.path.join(CODES_DIR, platform)
        entries: list[dict] = []

        if not os.path.isdir(platform_dir):
            print(f"  {platform}: directory not found, skipping")
            index[platform] = entries
            continue

        files = sorted(f for f in os.listdir(platform_dir) if f.endswith(".json"))

        for filename in files:
            code_str = filename[:-5]  # strip .json
            try:
                code = int(code_str)
            except ValueError:
                continue

            path = os.path.join(platform_dir, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                print(f"  ERROR {filename}: {exc}", file=sys.stderr)
                errors += 1
                continue

            entries.append(
                {
                    "code": code,
                    "manufacturer": data.get("manufacturer", "Unknown"),
                    "models": data.get("supportedModels", []),
                    "controller": data.get("supportedController", "Unknown"),
                    "encoding": data.get("commandsEncoding", "Unknown"),
                }
            )
            total += 1

        index[platform] = entries
        print(f"  {platform}: {len(entries)} devices")

    return index, total, errors


def main() -> None:
    print(f"Scanning {CODES_DIR} ...")
    index, total, errors = build_index()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    print(f"\nDone: {total} devices indexed")
    print(f"Output: {OUTPUT_FILE}")
    if errors:
        print(f"Warnings: {errors} files failed to parse", file=sys.stderr)


if __name__ == "__main__":
    main()
