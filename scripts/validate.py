#!/usr/bin/env python3
"""Validate the lifesim-packs registry with the Python standard library only.

Checks, in order:
  1. index.json parses and is internally consistent (unique ids, sane paths).
  2. Every index entry points at a real pack file, and every pack file on
     disk is listed in the index.
  3. Each pack file parses, its id/name/version/requires match the index
     entry, and the pack id matches its directory name.
  4. Structural sanity of pack contents (the lifesim engine's zod schema is
     authoritative — this is a lightweight gate for registry PRs).
  5. Dependency health: no self-requires, every `requires` target exists in
     the registry, version ranges are satisfied, and the graph is acyclic.

Usage: python3 scripts/validate.py [repo-root]   (default: repo root = script's parent)
Exit code 0 on success, 1 on any validation error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ID_RE = re.compile(r"^[\w:.-]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]*)?$")
RANGE_RE = re.compile(r"^(>=|<=|>|<|\^|~|=)?\s*(\d+(\.\d+){0,2})")
ALLOWED_TOP = {
    "id", "name", "version", "description", "requires",
    "events", "actions", "jobs", "laws", "items", "ailments", "names", "sections",
}
ALLOWED_REQ = {"pack", "version"}
CONDITION_KINDS = {
    "all", "any", "not", "age", "stat", "money", "trait", "flag",
    "chose", "counter", "person", "ailment", "job",
}
EFFECT_KINDS = {
    "stat", "money", "trait", "flag", "unflag", "counter", "collect",
    "loseitem", "die", "ailment", "cure", "rel", "relation", "person", "memory",
}
# World-law effects must be RNG-free and subject-free (mirrors the engine's refine).
WORLD_LAW_EFFECT_KINDS = {
    "stat", "money", "trait", "flag", "unflag", "counter", "collect",
    "loseitem", "die", "cure",
}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def parse_version(v: str):
    core = re.split(r"[-+]", v.strip())[0]
    parts = core.split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts) + (0, 0, 0)[: 3 - len(parts)]


def satisfies(version: str, range_expr: str) -> bool:
    """Minimal semver-range check matching the engine's satisfiesVersion."""
    v = parse_version(version)
    m = RANGE_RE.match(range_expr.strip())
    if v is None or m is None:
        return False
    op = m.group(1) or "="
    target = parse_version(m.group(2))
    if target is None:
        return False
    cmp = (v > target) - (v < target)
    if op == "=":
        return cmp == 0
    if op == ">=":
        return cmp >= 0
    if op == "<=":
        return cmp <= 0
    if op == ">":
        return cmp > 0
    if op == "<":
        return cmp < 0
    if op == "^":
        if v[0] != target[0]:
            return False
        if target[0] == 0:
            if v[1] != target[1]:
                return False
            if target[1] == 0:
                return v[2] == target[2]
        return cmp >= 0
    if op == "~":
        return v[0] == target[0] and v[1] == target[1] and cmp >= 0
    return False


def check_id(value: object, where: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or not ID_RE.match(value):
        err(f"{where}: invalid id {value!r} (ids must match {ID_RE.pattern})")


def check_condition(cond: object, where: str, depth: int = 0) -> None:
    if depth > 32:
        err(f"{where}: condition nesting too deep")
        return
    if not isinstance(cond, dict):
        err(f"{where}: condition must be an object")
        return
    kind = cond.get("kind")
    if kind not in CONDITION_KINDS:
        err(f"{where}: unknown condition kind {kind!r}")
        return
    if kind in ("all", "any"):
        subs = cond.get("conditions")
        if not isinstance(subs, list) or not subs:
            err(f"{where}: '{kind}' needs a non-empty 'conditions' list")
            return
        for i, c in enumerate(subs):
            check_condition(c, f"{where}.{kind}[{i}]", depth + 1)
    elif kind == "not":
        if "condition" not in cond:
            err(f"{where}: 'not' needs a 'condition'")
            return
        check_condition(cond["condition"], f"{where}.not", depth + 1)
    elif kind == "job":
        if "id" in cond:
            check_id(cond["id"], f"{where}.job.id")


def check_effects(effects: object, where: str, world_law: bool = False) -> None:
    if not isinstance(effects, list):
        err(f"{where}: 'effects' must be a list")
        return
    allowed = WORLD_LAW_EFFECT_KINDS if world_law else EFFECT_KINDS
    for i, e in enumerate(effects):
        if not isinstance(e, dict) or e.get("kind") not in allowed:
            err(f"{where}[{i}]: unknown or disallowed effect kind {e!r}")


def check_outcomes(outcomes: object, where: str) -> None:
    if not isinstance(outcomes, list) or not outcomes:
        err(f"{where}: 'outcomes' must be a non-empty list")
        return
    for i, o in enumerate(outcomes):
        if not isinstance(o, dict):
            err(f"{where}[{i}]: outcome must be an object")
            continue
        if not isinstance(o.get("result"), str) or not o["result"]:
            err(f"{where}[{i}]: outcome needs a non-empty 'result' string")
        if not isinstance(o.get("weight"), (int, float)) or o.get("weight", 0) < 0:
            err(f"{where}[{i}]: outcome needs a non-negative 'weight'")
        if "effects" in o:
            check_effects(o["effects"], f"{where}[{i}].effects")
        if "goto" in o:
            check_id(o["goto"], f"{where}[{i}].goto")


def check_pack(pack: dict, entry_path: str) -> None:
    where = entry_path
    extra = set(pack) - ALLOWED_TOP
    if extra:
        err(f"{where}: unknown top-level keys: {sorted(extra)}")

    check_id(pack.get("id"), f"{where}.id")
    if not isinstance(pack.get("name"), str) or not pack["name"]:
        err(f"{where}: 'name' must be a non-empty string")
    if not isinstance(pack.get("version"), str) or parse_version(pack["version"]) is None:
        err(f"{where}: 'version' must be a semver string like '1.0.0', got {pack.get('version')!r}")

    for req in pack.get("requires", []):
        if not isinstance(req, dict) or set(req) - ALLOWED_REQ:
            err(f"{where}: malformed requires entry {req!r}")
            continue
        check_id(req.get("pack"), f"{where}.requires.pack")
        if req.get("pack") == pack.get("id"):
            err(f"{where}: pack cannot require itself")
        if "version" in req and RANGE_RE.match(str(req["version"]).strip()) is None:
            err(f"{where}: bad version range {req['version']!r} in requires")

    ids_seen: set[str] = set()

    def track_id(section: str, i: int, obj: dict) -> None:
        oid = obj.get("id")
        check_id(oid, f"{where}.{section}[{i}].id")
        if isinstance(oid, str):
            if oid in ids_seen:
                err(f"{where}: duplicate id {oid!r}")
            ids_seen.add(oid)

    for i, ev in enumerate(pack.get("events", []) or []):
        w = f"{where}.events[{i}]"
        if not isinstance(ev, dict):
            err(f"{w}: event must be an object")
            continue
        track_id("events", i, ev)
        for f_ in ("title", "description"):
            if not isinstance(ev.get(f_), str) or not ev[f_]:
                err(f"{w}: event needs a non-empty '{f_}'")
        if "conditions" in ev:
            check_condition(ev["conditions"], f"{w}.conditions")
        choices = ev.get("choices")
        if not isinstance(choices, list) or not choices:
            err(f"{w}: event needs a non-empty 'choices' list")
            continue
        for j, ch in enumerate(choices):
            w2 = f"{w}.choices[{j}]"
            if not isinstance(ch, dict):
                err(f"{w2}: choice must be an object")
                continue
            check_id(ch.get("id"), f"{w2}.id")
            if not isinstance(ch.get("text"), str) or not ch["text"]:
                err(f"{w2}: choice needs non-empty 'text'")
            if "conditions" in ch:
                check_condition(ch["conditions"], f"{w2}.conditions")
            if "outcomes" in ch:
                check_outcomes(ch["outcomes"], f"{w2}.outcomes")
            if "effects" in ch:
                check_effects(ch["effects"], f"{w2}.effects")

    for i, act in enumerate(pack.get("actions", []) or []):
        w = f"{where}.actions[{i}]"
        if not isinstance(act, dict):
            err(f"{w}: action must be an object")
            continue
        track_id("actions", i, act)
        if not isinstance(act.get("title"), str) or not act["title"]:
            err(f"{w}: action needs a non-empty 'title'")
        if "conditions" in act:
            check_condition(act["conditions"], f"{w}.conditions")
        if "outcomes" in act:
            check_outcomes(act["outcomes"], f"{w}.outcomes")
        if "effects" in act:
            check_effects(act["effects"], f"{w}.effects")

    for i, job in enumerate(pack.get("jobs", []) or []):
        w = f"{where}.jobs[{i}]"
        if not isinstance(job, dict):
            err(f"{w}: job must be an object")
            continue
        track_id("jobs", i, job)
        if not isinstance(job.get("title"), str) or not job["title"]:
            err(f"{w}: job needs a non-empty 'title'")
        if not isinstance(job.get("salary"), int) or job["salary"] < 0:
            err(f"{w}: job needs a non-negative integer 'salary'")
        if "conditions" in job:
            check_condition(job["conditions"], f"{w}.conditions")

    for i, law in enumerate(pack.get("laws", []) or []):
        w = f"{where}.laws[{i}]"
        if not isinstance(law, dict):
            err(f"{w}: law must be an object")
            continue
        track_id("laws", i, law)
        if "conditions" in law:
            check_condition(law["conditions"], f"{w}.conditions")
        if "effects" in law:
            check_effects(law["effects"], f"{w}.effects", world_law=True)

    for i, item in enumerate(pack.get("items", []) or []):
        w = f"{where}.items[{i}]"
        if not isinstance(item, dict):
            err(f"{w}: item must be an object")
            continue
        track_id("items", i, item)
        if not isinstance(item.get("name"), str) or not item["name"]:
            err(f"{w}: item needs a non-empty 'name'")
        if not isinstance(item.get("price"), (int, float)) or item["price"] < 0:
            err(f"{w}: item needs a non-negative 'price'")
        if "conditions" in item:
            check_condition(item["conditions"], f"{w}.conditions")


def topo_order(packs: dict[str, dict]) -> list[str]:
    """Deterministic dependency-first order; ignores requires outside the set."""
    order: list[str] = []
    placed: set[str] = set()
    pending = list(packs)
    while pending:
        progressed = False
        still: list[str] = []
        for pid in pending:
            reqs = {r["pack"] for r in packs[pid].get("requires", [])} & set(packs)
            if reqs <= placed:
                order.append(pid)
                placed.add(pid)
                progressed = True
            else:
                still.append(pid)
        if not progressed:
            order.extend(still)  # cycles/missing deps were already reported
            break
        pending = still
    return order


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    index_path = root / "index.json"
    try:
        index = json.loads(index_path.read_text())
    except Exception as e:  # noqa: BLE001
        err(f"index.json: cannot parse — {e}")
        index = None

    entries = []
    if index is not None:
        if index.get("version") != 1:
            err("index.json: 'version' must be 1")
        entries = index.get("packs", [])
        if not isinstance(entries, list):
            err("index.json: 'packs' must be a list")
            entries = []

    seen_ids: set[str] = set()
    listed_paths: set[str] = set()
    for i, e in enumerate(entries):
        w = f"index.packs[{i}]"
        pid = e.get("id")
        check_id(pid, f"{w}.id")
        if pid in seen_ids:
            err(f"{w}: duplicate pack id {pid!r}")
        seen_ids.add(pid)
        path = e.get("path", "")
        expected = f"packs/{pid}/pack.json"
        if path != expected:
            err(f"{w}: path {path!r} must equal {expected!r} (directory name = pack id)")
        listed_paths.add(path)
        for f_ in ("name", "version", "description"):
            if f_ in e and not isinstance(e[f_], str):
                err(f"{w}: '{f_}' must be a string")

    packs: dict[str, dict] = {}
    for e in entries:
        pid = e.get("id")
        path = root / e.get("path", "")
        if not path.is_file():
            err(f"index.packs[{pid}]: file {e.get('path')!r} does not exist")
            continue
        try:
            pack = json.loads(path.read_text())
        except Exception as ex:  # noqa: BLE001
            err(f"{e.get('path')}: cannot parse — {ex}")
            continue
        packs[pid] = pack
        check_pack(pack, e.get("path", "?"))
        for f_ in ("id", "name", "version", "description"):
            if f_ in e and e[f_] != pack.get(f_):
                err(f"{e.get('path')}: {f_} {pack.get(f_)!r} does not match index value {e[f_]!r}")
        idx_reqs = e.get("requires", [])
        if idx_reqs != pack.get("requires", []):
            err(f"{e.get('path')}: requires list differs from index entry (index must mirror the pack file)")

    # pack files on disk missing from the index
    for pf in sorted((root / "packs").glob("*/pack.json")):
        rel = pf.relative_to(root).as_posix()
        if rel not in listed_paths:
            err(f"{rel}: pack file exists on disk but is not listed in index.json")
    # stray dirs without pack.json are fine (may be docs); stray pack.json outside packs/:
    for pf in sorted(root.rglob("pack.json")):
        rel = pf.relative_to(root).as_posix()
        if not rel.startswith("packs/"):
            err(f"{rel}: pack files must live under packs/<id>/pack.json")

    # dependency resolution against the registry
    for pid, pack in packs.items():
        for req in pack.get("requires", []):
            dep = req.get("pack")
            if dep in packs:
                rng = req.get("version")
                if rng and not satisfies(packs[dep].get("version", ""), rng):
                    err(
                        f"{pid}: requires {dep} {rng}, but registry has {dep} "
                        f"{packs[dep].get('version')!r}"
                    )
            else:
                err(f"{pid}: requires {dep!r}, which is not in the registry")

    # cycle detection
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(pid: str, trail: list[str]) -> None:
        if pid in done:
            return
        if pid in visiting:
            err(f"dependency cycle: {' -> '.join(trail + [pid])}")
            return
        visiting.add(pid)
        for req in packs.get(pid, {}).get("requires", []):
            dep = req.get("pack")
            if dep in packs:
                visit(dep, trail + [pid])
        visiting.discard(pid)
        done.add(pid)

    for pid in packs:
        visit(pid, [])

    if errors:
        print(f"FAILED: {len(errors)} problem(s)")
        for e_ in errors:
            print(f"  - {e_}")
        return 1

    order = topo_order(packs)
    print(f"OK: {len(packs)} pack(s) validated")
    print("install order: " + " -> ".join(order))
    return 0


if __name__ == "__main__":
    sys.exit(main())
