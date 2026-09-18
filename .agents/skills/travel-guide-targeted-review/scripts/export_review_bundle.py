#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.review.skill_bridge import export_review_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a frozen travel-guide review bundle.")
    parser.add_argument("project_id")
    parser.add_argument("finding_id")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    bundle = export_review_bundle(args.project_id, args.finding_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "bundle_sha256": bundle["bundle_sha256"],
        "scope_count": len(bundle["scope"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
