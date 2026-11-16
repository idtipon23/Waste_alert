#!/usr/bin/env python3
"""Build the static map site with a Maps key supplied through the environment."""

import argparse
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"


def build_site(output_dir):
    maps_key = os.environ.get("MAPS_KEY", "").strip()
    if not maps_key:
        raise ValueError("MAPS_KEY is required to build the public map")

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Build output already exists: {output_dir}")

    shutil.copytree(
        WEB_DIR,
        output_dir,
        ignore=shutil.ignore_patterns("config.js", "config.js.example"),
    )
    (output_dir / "config.js").write_text(
        f"window.MAPS_KEY = {json.dumps(maps_key)};\n",
        encoding="utf-8",
    )
    return output_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dist")
    args = parser.parse_args()
    print(f"Built static site at {build_site(args.output_dir)}")


if __name__ == "__main__":
    main()
