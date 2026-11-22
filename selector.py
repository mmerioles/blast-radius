"""Blast Radius selector: coverage-guided candidates, verified by execution.

For each patch:
  1. Diff the pristine and patched ASTs into change sites.
  2. Map each site to the tests that can reach it (per-test line coverage,
     class-instantiation and file-read maps built from the pinned commit).
  3. Run the cheap candidates against the patched tree (forked unittest child)
     and keep only the ones that actually fail. After the first failure the
     commit already pays a worker, so the rest are added blind when that is
     cheaper than verifying them.
  4. Expensive tests are never executed; they are added when the evidence says
     their chance of failing outweighs their cost.
  5. Split the chosen tests over workers to minimise makespan + 2s/worker.
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from br import overlay, runner  # noqa: E402
from br.analyze import Analyzer  # noqa: E402
from br.schedule import best_split  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
BASE = os.environ.get("BR_ROOT", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", "/opt/brmaps/maps.pickle")
WORK = os.environ.get("BR_WORK", "/tmp/br")
GIANT_S = float(os.environ.get("BR_GIANT", "3.0"))  # never execute tests this slow
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))  # our cpu-seconds per recorded second
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
START = time.monotonic()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def preload():
    """Import heavy third-party modules once so forked children share them."""
    for mod in (
        "unittest", "unittest.mock", "json", "ast", "csv", "datetime", "decimal", "sqlite3",
        "concurrent.futures", "multiprocessing", "tempfile", "pathlib", "importlib", "pkgutil",
        "gc", "tracemalloc", "typing", "dataclasses", "enum", "functools", "itertools", "re",
        "numpy", "pandas", "pandas.testing", "duckdb", "dateutil", "pytz",
    ):
        try:
            __import__(mod)
        except Exception:
            pass


class Plan:
    def __init__(self, cid, tests, sites, an):
        self.cid = cid
        self.sites = sites
        mask = 0
        for s in sites:
            mask |= s.mask
        self.cands = [i for i in range(an.n) if mask >> i & 1]
        sec = [t["seconds"] for t in tests]
        self.cheap = [i for i in self.cands if sec[i] < GIANT_S]
        self.giants = [i for i in self.cands if sec[i] >= GIANT_S]
        # per giant: list of (site_index) reaching it
        self.site_cheap = []
        for s in sites:
            self.site_cheap.append({i for i in self.cheap if s.mask >> i & 1})
        self.est = sum(sec[i] for i in self.cheap) * CPU_FACTOR
        self.probes = set()
        for s in sites:
            self.probes |= s.probes

    def giants_to_include(self, failed: set):
        out = []
        for g in self.giants:
            inc = False
            for k, s in enumerate(self.sites):
                if not (s.mask >> g & 1):
                    continue
                cheap = self.site_cheap[k]
                strong = bool(s.strong >> g & 1)
                if not cheap:
                    inc = True  # nothing cheaper can tell us about this site
                elif strong and (cheap & failed):
                    inc = True
                if inc:
                    break
            if inc:
                out.append(g)
        return out


def case_spec(an, sites, t):
    """Which fixture cases of test t can reach the change? None = run everything."""
    cases = an.cases_for(t)
    if cases is None:
        return None
    keys = cases["keys"]
    need = 0
    fixed = {}
    for site in sites:
        if not (site.mask >> t & 1):
            continue
        d = site.detail
        if d is None:
            return None
        if d[0] == "lines":
            _, rel, lines = d
            common = cases["common_lines"].get(rel, ())
            if any(l in common for l in lines):
                return None
            cl = cases["lines"].get(rel, {})
            m = 0
            for l in lines:
                m |= cl.get(l, 0)
            if not m:
                return None
            need |= m
        elif d[0] == "class":
            _, rel, qual, span = d
            mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
            key = f"{mod}:{qual}"
            if any(c == key or c.startswith(key + ".") for c in cases["common_classes"]):
                return None
            common = cases["common_lines"].get(rel, ())
            if any(l in common for l in span):
                return None
            m = 0
            for c, cm in cases["classes"].items():
                if c == key or c.startswith(key + "."):
                    m |= cm
            cl = cases["lines"].get(rel, {})
            for l in span:
                m |= cl.get(l, 0)
            if not m:
                return None
            need |= m
        elif d[0] == "fixture":
            _, name, flavours = d
            idxs = [idx for fname, idx in keys if fname == name]
            if not idxs:
                return None
            n_old = max(idxs) + 1
            match = [ch for cnt, ch in flavours if cnt == n_old]
            if len(match) != 1:
                return None
            fixed.setdefault(name, set()).update(match[0])
        else:
            return None
    spec = {}
    for j, (fname, idx) in enumerate(keys):
        spec.setdefault(fname, set())
        if need >> j & 1:
            spec[fname].add(idx)
    for fname, idxs in fixed.items():
        spec.setdefault(fname, set()).update(idxs)
    return spec


def ms(tests, i):
    return round(tests[i]["seconds"] * 1000)


def answer(tests, chosen):
    cost, workers = best_split([(ms(tests, i), i) for i in set(chosen)])
    return workers


def blind_answer(tests, plan):
    """No execution: everything reachable (used as fallback)."""
    return answer(tests, plan.cands)


# Self-calibrating CPU model: our CPU-seconds per recorded second (per worker).
CAL = {"meas": 2.0 * float(os.environ.get("BR_CPU_PRIOR", "1.5")), "rec": 2.0}


def cpu_factor():
    return CAL["meas"] / CAL["rec"]


def est_frac(an, spec, t):
    """Fraction of a test's recorded time we expect to spend with a case filter."""
    if spec is None:
        return 1.0
    cases = an.cases_for(t)
    if not cases:
        return 1.0
    n = len(cases["keys"])
    k = sum(len(v) for v in spec.values())
    return min(1.0, 0.15 + 0.85 * k / max(1, n))


def verify(tests, plan, root, deadline, an=None):
    """Run cheap candidates; return the chosen test set."""
    sec = [t["seconds"] for t in tests]
    specs = {i: (case_spec(an, plan.sites, i) if an is not None else None) for i in plan.cheap}
    est = {i: sec[i] * est_frac(an, specs[i], i) for i in plan.cheap}
    # cheapest (estimated) first: failures surface early and cheaply
    strong = 0
    for st in plan.sites:
        strong |= st.strong
    w = float(os.environ.get("BR_WEAK_W", "4"))
    order = sorted(plan.cheap, key=lambda i: (est[i] * (1.0 if strong >> i & 1 else w), sec[i]))
    failed = set()
    ran = set()
    state = {"blind": None}
    has_giants = bool(plan.giants)
    n_cheap = len(order)

    def blind_inc(rest):
        inc_g = plan.giants_to_include(failed) if failed else []
        base_set = failed | set(inc_g)
        base = best_split([(ms(tests, i), i) for i in base_set])[0] if base_set else 0
        full = best_split([(ms(tests, i), i) for i in base_set | set(rest)])[0]
        return (full - base) / 1000

    def on_result(idx, ok, secs):
        ran.add(idx)
        CAL["meas"] += secs
        CAL["rec"] += est[idx]
        if not ok:
            failed.add(idx)
        rest = [i for i in order if i not in ran]
        if not rest or state["blind"] is not None:
            return False
        if not failed and has_giants:
            # giants are only dropped once plenty of cheap evidence came back clean
            if len(ran) < max(20, n_cheap // 2):
                return False
        verify_cost = sum(est[i] for i in rest) * cpu_factor()
        if blind_inc(rest) < 0.8 * verify_cost:
            state["blind"] = rest
            return True
        return False

    # Nothing run yet: going blind right away is only safe without giants.
    if not has_giants and order:
        if blind_inc(order) < 0.8 * (sum(est[i] for i in order) * cpu_factor() + 0.15):
            return set(order) | set(plan.giants_to_include(set()))

    ids = [(i, tests[i]["id"], 4.0 + 6.0 * sec[i], specs[i]) for i in order]
    probe_fail = set()
    res = runner.run(root, ids, deadline=deadline, on_result=on_result,
                     probes=sorted(plan.probes), on_probe=probe_fail.add)
    for i, ok in res.items():
        if not ok:
            failed.add(i)
    extra_chosen = set()
    if probe_fail and an is not None:
        # a module no longer imports: everything importing it (transitively) breaks
        m = 0
        for rel in probe_fail:
            m |= an.import_closure(rel)
        more = [i for i in range(len(tests)) if m >> i & 1 and i not in res]
        cheap_more = [i for i in more if sec[i] < GIANT_S]
        extra_chosen |= {i for i in more if sec[i] >= GIANT_S}
        log(f"{plan.cid}: import broken in {sorted(probe_fail)} -> +{len(more)} tests")
        if cheap_more:
            res2 = runner.run(root, [(i, tests[i]["id"], 4.0 + 6.0 * sec[i]) for i in cheap_more], deadline=deadline)
            extra_chosen |= {i for i, ok in res2.items() if not ok}
            extra_chosen |= {i for i in cheap_more if i not in res2}
    chosen = set(failed)
    if state["blind"] is not None:
        chosen |= set(i for i in state["blind"] if i not in res)
    else:
        # anything not reported (deadline / crash) is included blind
        chosen |= set(i for i in order if i not in res)
    chosen |= set(plan.giants_to_include(failed if res else set(plan.cheap)))
    chosen |= extra_chosen
    return chosen


def handle(cid, tests, an):
    text = open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="surrogateescape").read()
    files, _ = overlay.patched_files(BASE, text)
    sites = an.analyze(files)
    plan = Plan(cid, tests, sites, an)
    return plan, files


def _child_cpu():
    import resource
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def process(cid, tests, an, deadline):
    t0 = time.monotonic()
    c0 = _child_cpu()
    plan, files = handle(cid, tests, an)
    if not plan.cands:
        log(f"{cid}: no candidates ({len(plan.sites)} sites)")
        return []
    if not plan.cheap:
        chosen = plan.giants_to_include(set())
        log(f"{cid}: giants only -> {len(chosen)}")
        return answer(tests, chosen)
    root = overlay.build(BASE, os.path.join(WORK, cid), {k: v[1] for k, v in files.items()})
    try:
        chosen = verify(tests, plan, root, deadline, an)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    log(f"{cid}: cands={len(plan.cands)} cheap={len(plan.cheap)} est={plan.est:.2f}s "
        f"chosen={len(chosen)} in {time.monotonic() - t0:.2f}s cpu={_child_cpu() - c0:.2f}s")
    return answer(tests, chosen)


def worker_loop(rfd, wfd, tests, an, deadline):
    rf = os.fdopen(rfd, "r")
    wf = os.fdopen(wfd, "w", buffering=1)
    for line in rf:
        cid = line.strip()
        try:
            workers = process(cid, tests, an, deadline)
        except Exception:
            log(f"{cid}: error\n{traceback.format_exc()}")
            workers = None
        wf.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
        wf.flush()


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    cids = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    out = {}
    fallback = {}
    try:
        with open(MAPS, "rb") as f:
            maps = pickle.load(f)
        an = Analyzer(maps, tests)
    except Exception:
        log(f"maps unavailable, running everything\n{traceback.format_exc()}")
        allw = answer(tests, range(len(tests)))
        for cid in cids:
            print(json.dumps({"commit": cid, "workers": allw}))
        return
    limit = float(os.environ.get("BR_LIMIT", "160"))  # practice run limit is 180s
    deadline = START + limit
    os.makedirs(WORK, exist_ok=True)

    # plan everything first (cheap) so we always have a fallback
    plans = {}
    for cid in cids:
        try:
            plan, _ = handle(cid, tests, an)
            plans[cid] = plan
            fallback[cid] = blind_answer(tests, plan)
        except Exception:
            log(f"{cid}: plan error\n{traceback.format_exc()}")
            fallback[cid] = answer(tests, range(len(tests)))
    preload()

    # cheap commits first: they calibrate the CPU model before the expensive decisions
    jobs = sorted(cids, key=lambda c: (plans[c].est if c in plans else 0))
    import select

    workers = []
    for _ in range(max(1, min(PARALLEL, len(jobs)))):
        a_r, a_w = os.pipe()  # parent -> worker
        b_r, b_w = os.pipe()  # worker -> parent
        pid = os.fork()
        if pid == 0:
            os.close(a_w)
            os.close(b_r)
            try:
                worker_loop(a_r, b_w, tests, an, deadline)
            finally:
                os._exit(0)
        os.close(a_r)
        os.close(b_w)
        workers.append({"pid": pid, "w": os.fdopen(a_w, "w", buffering=1), "r": b_r, "buf": b"", "job": None})

    def assign(w):
        if jobs:
            cid = jobs.pop(0)
            w["job"] = cid
            w["w"].write(cid + "\n")
            w["w"].flush()
        else:
            w["job"] = None
            try:
                w["w"].close()
            except Exception:
                pass

    for w in workers:
        assign(w)
    while any(w["job"] for w in workers):
        live = [w for w in workers if w["job"]]
        timeout = deadline + 8 - time.monotonic()
        if timeout <= 0:
            break
        r, _, _ = select.select([w["r"] for w in live], [], [], timeout)
        for w in live:
            if w["r"] not in r:
                continue
            chunk = os.read(w["r"], 1 << 20)
            if not chunk:
                w["job"] = None
                continue
            w["buf"] += chunk
            while b"\n" in w["buf"]:
                line, w["buf"] = w["buf"].split(b"\n", 1)
                try:
                    d = json.loads(line)
                    if d.get("workers") is not None:
                        out[d["commit"]] = d["workers"]
                except Exception:
                    pass
                assign(w)
    for w in workers:
        try:
            os.kill(w["pid"], 9)
        except Exception:
            pass
        try:
            os.waitpid(w["pid"], 0)
        except Exception:
            pass
    for cid in cids:
        ws = out.get(cid)
        if ws is None:
            ws = fallback[cid]
            log(f"{cid}: fallback")
        ws = [list(map(int, w)) for w in ws if w][:8]
        print(json.dumps({"commit": cid, "workers": ws}))
    sys.stdout.flush()
    t = os.times()
    log(f"done in {time.monotonic() - START:.1f}s wall; cpu self {t.user + t.system:.1f}s "
        f"children {t.children_user + t.children_system:.1f}s")
    log(f"BR_CPU_SECONDS={t.user + t.system + t.children_user + t.children_system:.2f}")
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    log(f"BR_CGROUP_CPU_SECONDS={int(line.split()[1]) / 1e6:.2f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
