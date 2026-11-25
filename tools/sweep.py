"""Run workloads over several seeds and report the worst value of each metric.

    python tools/sweep.py                         # basic + workloads/*.yaml, seeds 1-5
    python tools/sweep.py -w loss_5 medium_incast # just these
    python tools/sweep.py --seeds 1 2 -j 4 --protocol example/baseline_stopwait

Grading keeps the worst seed, so this does too. Runs go to runs/sweep/.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = "datacenter"
# Metric key -> (path in results.json, True if higher is better)
METRICS = {
    "reliability": (("metrics", "reliability", "ratio"), True),
    "slowdown": (("requirements", "checked", "step_slowdown.mean_max", "actual"), False),
    "overhead": (("metrics", "overhead", "ratio"), False),
}
RUN_TIMEOUT_S = 120
BOUNDS = {"reliability": 1.0, "slowdown": 2.5, "overhead": 1.30}


def dig(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def workload_arg(name: str) -> str:
    return "basic_workload" if name == "basic" else str(ROOT / "workloads" / f"{name}.yaml")


def run_one(name: str, seed: int, protocol: str) -> dict:
    out = ROOT / "runs" / "sweep" / f"{name}-s{seed}"
    cmd = [sys.executable, "-m", "transportlab", "run", "--scenario", SCENARIO,
           "--workload", workload_arg(name), "--protocol", protocol,
           "--out", str(out), "--seed", str(seed), "--runner", "ns3"]
    res = {"name": name, "seed": seed, "out": out}
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        res["error"] = [f"timed out after {RUN_TIMEOUT_S}s wall clock (timer loop?)"]
        return res
    try:
        r = json.loads((out / "results.json").read_text())
    except (OSError, json.JSONDecodeError):
        res["error"] = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no results.json"]
        return res
    for key, (path, _) in METRICS.items():
        res[key] = dig(r, path)
    res["crashed"] = bool(r.get("crashed_endpoints"))
    if res["crashed"]:
        for ep in r["crashed_endpoints"]:
            try:
                evs = json.loads((out / f"endpoint-{ep}.json").read_text())
                evs = evs.get("events", evs) if isinstance(evs, dict) else evs
                exc = next(e for e in evs if e.get("kind") == "protocol_exception")
                res["error"] = [f"{ep} in {exc.get('where')}: {exc.get('error')}"]
                break
            except (OSError, json.JSONDecodeError, StopIteration, AttributeError):
                continue
    try:
        links = json.loads((out / "sim-stats.json").read_text())["_links"]
        res["qdrops"] = sum(l.get("queue_drops", 0) for l in links.values())
    except (OSError, json.JSONDecodeError, KeyError):
        res["qdrops"] = None
    return res


def worst(vals, higher_better):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return min(vals) if higher_better else max(vals)


def fmt(key, v):
    if v is None:
        return "   -   "
    mark = "" if key not in BOUNDS else (
        " " if (v >= BOUNDS[key] if METRICS[key][1] else v <= BOUNDS[key]) else "!")
    return f"{v:7.3f}{mark}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-w", "--workloads", nargs="*",
                    help="names in workloads/ (no .yaml), or 'basic'")
    ap.add_argument("--seeds", nargs="*", type=int, default=[1, 2, 3, 4, 5])
    ap.add_argument("--protocol", default="protocol.py")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    args = ap.parse_args()

    names = args.workloads or ["basic"] + sorted(p.stem for p in (ROOT / "workloads").glob("*.yaml"))
    jobs = [(n, s) for n in names for s in args.seeds]
    with ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(lambda j: run_one(*j, args.protocol), jobs))

    print(f"{'workload':<18} {'reliab':>8} {'slowdown':>8} {'overhead':>8} {'qdrops':>7}  (worst of seeds {args.seeds}; ! = fails)")
    failed = False
    for n in names:
        rs = [r for r in results if r["name"] == n]
        errs = [r for r in rs if "error" in r or r.get("crashed")]
        row = {k: worst([r.get(k) for r in rs], hb) for k, (_, hb) in METRICS.items()}
        qd = max((r.get("qdrops") or 0 for r in rs), default=0)
        print(f"{n:<18} {fmt('reliability', row['reliability'])} {fmt('slowdown', row['slowdown'])} "
              f"{fmt('overhead', row['overhead'])} {qd:>7}")
        for r in errs:
            print(f"    seed {r['seed']}: {r.get('error') or 'crashed endpoint'}  ({r['out']})")
        failed |= bool(errs) or any(
            row[k] is None or not (row[k] >= b if METRICS[k][1] else row[k] <= b) for k, b in BOUNDS.items())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
