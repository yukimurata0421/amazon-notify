from __future__ import annotations

import argparse
import json
import sys

from ..runtime import RuntimeConfig
from ..scenarios import list_scenarios, run_scenarios


def handle_scenario_harness(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.scenario_harness:
        return False

    names = [x.strip() for x in args.scenario_names.split(",") if x.strip()]
    selected = names or list_scenarios()
    results = run_scenarios(runtime, selected)

    output = {
        "status": "ok" if all(r.ok for r in results) else "degraded",
        "scenario_count": len(results),
        "results": [
            {
                "name": r.name,
                "ok": r.ok,
                "details": r.details,
            }
            for r in results
        ],
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if output["status"] == "ok" else 1)
