"""Static checks for n8n workflow export files. No dependencies.

Checks: valid JSON; the top-level keys n8n's export format uses; every node has
id/name/type/typeVersion/position/parameters; node names and ids are unique;
every connection points at an existing node; no credentials or obvious secrets
are embedded; the workflow is exported inactive.

Usage: python tools/validate_workflow.py workflows/*.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_TOP = {"name": str, "nodes": list, "connections": dict, "settings": dict}
REQUIRED_NODE = {"id": str, "name": str, "type": str, "typeVersion": (int, float), "position": list, "parameters": dict}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Z0-9/]+"),
]


def check(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        wf = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]

    for key, typ in REQUIRED_TOP.items():
        if not isinstance(wf.get(key), typ):
            errors.append(f"top-level '{key}' missing or not {typ.__name__}")
    if errors:
        return errors

    if wf.get("active") not in (False, None):
        errors.append("export should be inactive ('active': false)")

    names, ids = set(), set()
    for i, node in enumerate(wf["nodes"]):
        label = node.get("name", f"#{i}")
        for key, typ in REQUIRED_NODE.items():
            if not isinstance(node.get(key), typ):
                errors.append(f"node {label}: '{key}' missing or wrong type")
        if not str(node.get("type", "")).startswith(("n8n-nodes-base.", "@n8n/")):
            errors.append(f"node {label}: unexpected type {node.get('type')}")
        pos = node.get("position", [])
        if len(pos) != 2 or not all(isinstance(v, (int, float)) for v in pos):
            errors.append(f"node {label}: position must be [x, y]")
        if node.get("credentials"):
            errors.append(f"node {label}: embedded credentials reference; remove before publishing")
        if label in names:
            errors.append(f"duplicate node name {label}")
        if node.get("id") in ids:
            errors.append(f"duplicate node id {node.get('id')}")
        names.add(label)
        ids.add(node.get("id"))

    for source, outputs in wf["connections"].items():
        if source not in names:
            errors.append(f"connection from unknown node {source}")
        for conn_type, branches in outputs.items():
            for branch in branches:
                for target in branch or []:
                    if target.get("node") not in names:
                        errors.append(f"{source} -> unknown node {target.get('node')}")
                    if target.get("type") != conn_type or not isinstance(target.get("index"), int):
                        errors.append(f"{source} -> {target.get('node')}: malformed connection")

    text = path.read_text()
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"possible secret matches /{pattern.pattern}/")
    return errors


def main(paths: list[str]) -> int:
    failed = False
    for p in paths:
        errs = check(Path(p))
        if errs:
            failed = True
            print(f"FAIL {p}")
            for e in errs:
                print(f"  - {e}")
        else:
            wf = json.loads(Path(p).read_text())
            print(f"OK   {p}  ({len(wf['nodes'])} nodes, {sum(len(v) for v in wf['connections'].values())} connected sources)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [str(p) for p in Path("workflows").glob("*.json")]))
