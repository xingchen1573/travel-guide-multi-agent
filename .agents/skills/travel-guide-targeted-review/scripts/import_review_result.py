#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.review.skill_bridge import import_review_result


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a validated travel-guide review recommendation.")
    parser.add_argument("project_id")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()

    recommendation = import_review_result(
        args.project_id,
        _read_json(args.bundle),
        _read_json(args.result),
    )
    print(json.dumps(recommendation.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
