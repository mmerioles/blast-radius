"""Build per-test dependency maps for the pristine sqlglot checkout.

Usage: python tools/build_maps.py <sqlglot_root> <tests.json> <out.pickle> [shards]

Maps (test sets are int bitmasks over test numbers):
  lines:   {relfile: {line: mask}}  runtime lines (function/lambda bodies only)
  classes: {"module:QualName": mask} classes instantiated (type(self) at __init__)
  files:   {relpath: mask}          non-import file reads / dir listings
"""

import json
import os
import pickle
import sys
import time

ROOT = os.path.abspath(sys.argv[1])
TESTS = json.load(open(sys.argv[2]))["tests"]
OUT = sys.argv[3]
SHARDS = int(sys.argv[4]) if len(sys.argv) > 4 else 4
MIN_CASE_S = 0.05
T0 = time.monotonic()
# The image build has 10 minutes in total; optional passes are skipped when slow.
BUDGET = float(os.environ.get("BR_MAP_BUDGET", "330"))
REVERSE = [False]  # keep per-case maps only for tests worth filtering

CO_OPTIMIZED = 0x0001


class _DoneFuture:
    pass


def make_sync_executor(get_key=None, switch=None):
    import concurrent.futures as cf
    import pickle as pk

    class KeyedFuture(cf.Future):
        _br_key = None

        def result(self, timeout=None):
            if switch is not None:
                switch(self._br_key)
            return super().result(timeout)

    class SyncExecutor:
        def __init__(self, *a, initializer=None, initargs=(), **kw):
            if initializer is not None:
                init, args = pk.loads(pk.dumps((initializer, initargs)))
                init(*args)

        def submit(self, fn, *args, **kwargs):
            f = KeyedFuture()
            f._br_key = get_key() if get_key else None
            try:
                fn2, args2, kwargs2 = pk.loads(pk.dumps((fn, args, kwargs)))
                r = fn2(*args2, **kwargs2)
                f.set_result(pk.loads(pk.dumps(r)))
            except BaseException as e:  # noqa
                f.set_exception(e)
            return f

        def map(self, fn, *iterables, **kw):
            return [self.submit(fn, *a).result() for a in zip(*iterables)]

        def shutdown(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.shutdown()

    return SyncExecutor


def shard_main(shard_tests, out_path):
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    os.environ["PYTHONPATH"] = ROOT
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    import types
    import unittest

    sys.modules["__main__"] = types.ModuleType("__main__")
    sys.argv = ["python"]

    root_prefix = ROOT + os.sep
    # coverage is segmented by fixture case: key None = common (non-case) code
    segs = {}
    S = [set()]  # active segment's line set
    C = [set()]  # active segment's class set
    active = [None]
    cur_files = set()
    per_test = {}

    def switch(key):
        if key == active[0] and key in segs:
            return
        active[0] = key
        seg = segs.get(key)
        if seg is None:
            seg = segs[key] = (set(), set())
        S[0], C[0] = seg
        mon.restart_events()

    def reset_segments():
        segs.clear()
        active[0] = "__none__"
        switch(None)

    mon = sys.monitoring
    TOOL = mon.COVERAGE_ID
    DISABLE = mon.DISABLE
    file_cache = {}

    def relname(fn):
        r = file_cache.get(fn)
        if r is None:
            if fn.startswith(root_prefix):
                r = fn[len(root_prefix):]
                if not (r.startswith("sqlglot/") or r.startswith("tests/")):
                    r = ""
            else:
                r = ""
            file_cache[fn] = r
        return r

    IMPORTING = []  # stack of module rel paths currently executing their body
    import_lines = {}  # (rel, line) -> set(importer rel)

    def on_line(code, line):
        if not (code.co_flags & CO_OPTIMIZED):
            return DISABLE
        r = relname(code.co_filename)
        if IMPORTING:
            if r:
                k = (r, line)
                st = import_lines.get(k)
                if st is None:
                    st = import_lines[k] = set()
                st.add(IMPORTING[-1])
                S[0].add(k)
            return None  # keep firing: other importers must be recorded too
        if r:
            S[0].add((r, line))
        return DISABLE

    # file access audit
    def in_import():
        f = sys._getframe(2)
        while f is not None:
            fn = f.f_code.co_filename
            if fn.startswith("<frozen importlib") or "importlib" in fn and fn.endswith(".py") and "/importlib/" in fn:
                return True
            f = f.f_back
        return False

    def source_for_display():
        """linecache reads made while formatting tracebacks/warnings are not
        dependencies; inspect.getsource (also linecache) is."""
        f = sys._getframe(2)
        lc = False
        while f is not None:
            fn = f.f_code.co_filename
            if fn.endswith("inspect.py"):
                return False
            if fn.endswith("linecache.py") or fn.endswith("traceback.py"):
                lc = True
            f = f.f_back
        return lc

    tracing = [False]

    def audit(event, args):
        if not tracing[0]:
            return
        if event in ("open", "os.listdir", "os.scandir", "glob.glob"):
            p = args[0] if args else None
            if isinstance(p, bytes):
                try:
                    p = p.decode()
                except Exception:
                    return
            if not isinstance(p, str):
                return
            if event == "open" and (p.endswith(".pyc") or p.endswith(".so")):
                return
            ap = os.path.abspath(p)
            ap = os.path.realpath(ap) if not ap.startswith(root_prefix) else ap
            if not ap.startswith(root_prefix):
                return
            if event == "open" and in_import():
                return
            if event == "open" and ap.endswith(".py") and source_for_display():
                return
            cur_files.add(ap[len(root_prefix):])

    sys.addaudithook(audit)

    import importlib._bootstrap_external as ibe

    orig_exec = ibe.SourceFileLoader.exec_module

    def exec_module(self, module):
        r = relname(os.path.abspath(getattr(module, "__file__", "") or ""))
        if not r:
            return orig_exec(self, module)
        IMPORTING.append(r)
        mon.restart_events()
        try:
            return orig_exec(self, module)
        finally:
            IMPORTING.pop()
            mon.restart_events()

    ibe.SourceFileLoader.exec_module = exec_module
    # start monitoring before any sqlglot import so import-time code is seen
    mon.use_tool_id(TOOL, "brmap")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)
    for mname in [m for m in list(sys.modules) if m == "sqlglot" or m.startswith("sqlglot.") or m.startswith("tests")]:
        del sys.modules[mname]

    # import test modules up front
    mods = {}
    for t in shard_tests:
        path, cls, meth = t["id"].split("::")
        mod = path[:-3].replace("/", ".")
        mods.setdefault(mod, None)
    SyncExecutor = make_sync_executor(lambda: active[0], switch)

    def wrap_loader(orig):
        if getattr(orig, "_br_wrapped", False):
            return orig

        def loader(filename, *a, **kw):
            for idx, item in enumerate(orig(filename, *a, **kw)):
                switch((filename, idx))
                yield item
            switch(None)

        loader._br_wrapped = True
        return loader

    for mod in mods:
        try:
            __import__(mod)
            m = sys.modules[mod]
            if hasattr(m, "ProcessPoolExecutor"):
                m.ProcessPoolExecutor = SyncExecutor
            for lname in ("load_sql_fixture_pairs", "load_sql_fixtures"):
                if callable(getattr(m, lname, None)):
                    setattr(m, lname, wrap_loader(getattr(m, lname)))
        except BaseException as e:
            print("import fail", mod, e, file=sys.stderr)

    # wrap __init__ of every sqlglot class loaded so far (subclasses created later
    # still go through wrapped base __init__ when they call super()).
    import functools

    wrapped = set()

    def wrap_all():
        for mname, m in list(sys.modules.items()):
            if not mname.startswith("sqlglot") or m is None:
                continue
            for obj in list(vars(m).values()):
                if isinstance(obj, type) and obj.__module__ == mname:
                    stack = [obj]
                    while stack:
                        c = stack.pop()
                        for v in vars(c).values():
                            if isinstance(v, type) and v.__module__ == mname and v.__qualname__.startswith(c.__qualname__ + "."):
                                stack.append(v)
                        init = vars(c).get("__init__")
                        if init is None or id(c) in wrapped or not isinstance(init, types.FunctionType):
                            continue
                        wrapped.add(id(c))

                        def mk(orig):
                            @functools.wraps(orig)
                            def __init__(self, *a, **kw):
                                C[0].add(type(self))
                                return orig(self, *a, **kw)

                            return __init__

                        try:
                            setattr(c, "__init__", mk(init))
                        except (TypeError, AttributeError):
                            pass

    wrap_all()

    by_class = {}
    order = []
    if REVERSE[0]:
        shard_tests = list(reversed(shard_tests))
    for t in shard_tests:
        path, cls, meth = t["id"].split("::")
        mod = path[:-3].replace("/", ".")
        key = (mod, cls)
        if key not in by_class:
            by_class[key] = []
            order.append(key)
        by_class[key].append((t["number"], meth))

    def class_keys(cs):
        classes = set()
        for c in cs:
            for b in c.__mro__:
                if b.__module__.startswith("sqlglot"):
                    classes.add(f"{b.__module__}:{b.__qualname__}")
        return frozenset(classes)

    def snapshot():
        """-> (common_lines, common_classes, files, {case_key: (lines, classes)})"""
        common = segs.get(None, (set(), set()))
        cases = {k: (frozenset(v[0]), class_keys(v[1])) for k, v in segs.items() if k is not None}
        return (frozenset(common[0]), class_keys(common[1]), frozenset(cur_files), cases)

    for mod, cls in order:
        m = sys.modules.get(mod)
        klass = getattr(m, cls, None) if m else None
        if klass is None:
            continue
        # setUpClass coverage is attributed to every test of the class
        reset_segments(); cur_files.clear()
        tracing[0] = True
        try:
            klass.setUpClass()
        except BaseException as e:
            print("setUpClass fail", cls, e, file=sys.stderr)
        tracing[0] = False
        cls_snap = snapshot()
        cls_snap = (cls_snap[0] | frozenset(l for v in cls_snap[3].values() for l in v[0]),
                    cls_snap[1] | frozenset(c for v in cls_snap[3].values() for c in v[1]),
                    cls_snap[2], {})
        for num, meth in by_class[(mod, cls)]:
            reset_segments(); cur_files.clear()
            res = unittest.TestResult()
            tracing[0] = True
            t0 = time.perf_counter()
            try:
                klass(meth).run(res)
            except BaseException as e:
                print("run fail", meth, e, file=sys.stderr)
            dt = time.perf_counter() - t0
            tracing[0] = False
            wrap_all()  # newly lazy-loaded dialect modules
            s = snapshot()
            ok = not (res.errors or res.failures)
            per_test[num] = (s[0] | cls_snap[0], s[1] | cls_snap[1], s[2] | cls_snap[2], s[3], ok, dt)
        try:
            klass.tearDownClass()
            klass.doClassCleanups()
        except BaseException:
            pass
    mon.set_events(TOOL, 0)
    with open(out_path, "wb") as f:
        pickle.dump((per_test, import_lines), f)


def static_importers(root, top_only=False):
    """{module rel: set(rel of modules that import it)}; top_only: module-level imports only"""
    import ast

    files = {}
    for top in ("sqlglot", "tests"):
        for d, _, fs in os.walk(os.path.join(root, top)):
            for f in fs:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(d, f), root)
                    files[rel] = None

    def resolve(modname):
        out = []
        base = modname.replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in files:
                out.append(cand)
        return out

    rev = {}
    for rel in files:
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except Exception:
            continue
        pkg = rel[:-3].replace("/", ".")
        if rel.endswith("__init__.py"):
            pkg = pkg[: -len(".__init__")]
        else:
            pkg = pkg.rpartition(".")[0]
        if top_only:
            nodes = []
            stack = list(tree.body)
            while stack:
                n = stack.pop()
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                nodes.append(n)
                stack.extend(ast.iter_child_nodes(n))
        else:
            nodes = ast.walk(tree)
        for node in nodes:
            targets = []
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    for i in range(1, len(parts) + 1):
                        targets += resolve(".".join(parts[:i]))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                parts = mod.split(".")
                for i in range(1, len(parts) + 1):
                    targets += resolve(".".join(parts[:i]))
                for a in node.names:
                    targets += resolve(mod + "." + a.name)
            for t in targets:
                if t != rel:
                    rev.setdefault(t, set()).add(rel)
    return {k: frozenset(v) for k, v in rev.items()}


def timing_pass():
    """Run every test once through the selector's own runner, untraced."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import runner

    shards = [[] for _ in range(SHARDS)]
    loads = [0.0] * SHARDS
    for t in sorted(TESTS, key=lambda t: -t["seconds"]):
        j = loads.index(min(loads))
        shards[j].append(t)
        loads[j] += t["seconds"]
    tmp = OUT + ".tshard"
    pids = []
    for i, sh in enumerate(shards):
        sh.sort(key=lambda t: t["id"])
        pid = os.fork()
        if pid == 0:
            try:
                got = {}
                ids = [(t["number"], t["id"], 120.0) for t in sh]
                runner.run(ROOT, ids, on_result=lambda n, ok, secs: got.__setitem__(n, secs))
                with open(f"{tmp}{i}", "wb") as f:
                    pickle.dump(got, f)
            finally:
                os._exit(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    cost = {}
    for i in range(SHARDS):
        try:
            with open(f"{tmp}{i}", "rb") as f:
                cost.update(pickle.load(f))
            os.remove(f"{tmp}{i}")
        except FileNotFoundError:
            pass
    return cost


def main():
    # shard by test class, balancing recorded seconds
    groups = {}
    for t in TESTS:
        key = t["id"].rsplit("::", 1)[0]
        groups.setdefault(key, []).append(t)
    shards = [[] for _ in range(SHARDS)]
    loads = [0.0] * SHARDS
    for key, ts in sorted(groups.items(), key=lambda kv: -sum(t["seconds"] for t in kv[1])):
        j = loads.index(min(loads))
        shards[j].extend(ts)
        loads[j] += sum(t["seconds"] for t in ts)
    def collect(reverse):
        REVERSE[0] = reverse
        pids = []
        tmp = OUT + (".rshard" if reverse else ".shard")
        for i, sh in enumerate(shards):
            pid = os.fork()
            if pid == 0:
                try:
                    shard_main(sh, f"{tmp}{i}")
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        per_test = {}
        import_lines = {}
        for i in range(SHARDS):
            with open(f"{tmp}{i}", "rb") as f:
                pt, il = pickle.load(f)
                per_test.update(pt)
                for k, v in il.items():
                    import_lines.setdefault(k, set()).update(v)
            os.remove(f"{tmp}{i}")
        return per_test, import_lines

    per_test, import_lines = collect(False)
    order_dep = {}
    fwd_s = time.monotonic() - T0
    print(f"forward pass {fwd_s:.0f}s", file=sys.stderr)
    if os.environ.get("BR_BOTH", "1") == "1" and fwd_s * 2.2 < BUDGET:
        # Second pass in reverse order: lines whose test set changes depend on
        # run-once state (caches, lazy loading) and are flagged.
        rev_tests, rev_imports = collect(True)

        def line_sets(pt):
            out = {}
            for num, v in pt.items():
                ls = set(v[0])
                for seg in v[3].values():
                    ls |= seg[0]
                for k in ls:
                    out[k] = out.get(k, 0) | (1 << num)
            return out

        fa, fb = line_sets(per_test), line_sets(rev_tests)
        for k, v in rev_imports.items():
            import_lines.setdefault(k, set()).update(v)
        for k in set(fa) | set(fb):
            # import-time lines are handled by import tracking + probes
            if fa.get(k, 0) != fb.get(k, 0) and k not in import_lines:
                order_dep.setdefault(k[0], set()).add(k[1])
        # union coverage segment by segment (common with common, case k with case k)
        for num, v in rev_tests.items():
            if num not in per_test:
                per_test[num] = v
                continue
            fwd = per_test[num]
            cases = dict(fwd[3])
            for k, (ls, cs) in v[3].items():
                if k in cases:
                    cases[k] = (cases[k][0] | ls, cases[k][1] | cs)
                else:
                    cases[k] = (ls, cs)
            per_test[num] = (fwd[0] | v[0], fwd[1] | v[1], fwd[2] | v[2], cases, fwd[4], fwd[5])

    imp = {}
    for (r, l), v in import_lines.items():
        imp.setdefault(r, {})[l] = frozenset(v)

    intern = {}

    def I(v):
        return intern.setdefault(v, v)

    lines, classes, files = {}, {}, {}
    cases = {}
    bad = []
    for num, (ls, cs, fs, segcases, ok, dt) in per_test.items():
        bit = 1 << num
        if not ok:
            bad.append(TESTS[num]["id"])
        all_ls = set(ls)
        all_cs = set(cs)
        for v in segcases.values():
            all_ls |= v[0]
            all_cs |= v[1]
        for f, l in all_ls:
            d = lines.setdefault(f, {})
            d[l] = d.get(l, 0) | bit
        for c in all_cs:
            classes[c] = classes.get(c, 0) | bit
        for f in fs:
            files[f] = files.get(f, 0) | bit
        if len(segcases) >= 2 and TESTS[num]["seconds"] >= MIN_CASE_S:
            keys = sorted(segcases)
            cl, cc = {}, {}
            for j, k in enumerate(keys):
                b = 1 << j
                for f, l in segcases[k][0]:
                    d = cl.setdefault(f, {})
                    d[l] = d.get(l, 0) | b
                for c in segcases[k][1]:
                    cc[c] = cc.get(c, 0) | b
            common_lines = {}
            for f, l in ls:
                common_lines.setdefault(f, set()).add(l)
            cases[num] = {
                "keys": keys,
                "common_lines": {f: frozenset(v) for f, v in common_lines.items()},
                "common_classes": frozenset(cs),
                "lines": cl,
                "classes": cc,
            }
    for f in lines:
        lines[f] = {l: I(m) for l, m in lines[f].items()}
    classes = {k: I(v) for k, v in classes.items()}
    files = {k: I(v) for k, v in files.items()}
    missing = [t["id"] for t in TESTS if t["number"] not in per_test]
    data = {"lines": lines, "classes": classes, "files": files, "n": len(TESTS), "cases": cases,
            "import_lines": imp, "importers": static_importers(ROOT),
            "importers_top": static_importers(ROOT, top_only=True),
            "order_dep": {f: frozenset(v) for f, v in order_dep.items()},
            # seconds per test in the real runner (no tracing): relative CPU cost
            "cost": timing_pass() if time.monotonic() - T0 < BUDGET * 0.7 else {},
            "ids": [t["id"] for t in TESTS]}
    with open(OUT, "wb") as f:
        pickle.dump(data, f, protocol=5)
    os.chmod(OUT, 0o644)
    print(f"maps: {len(per_test)} tests, {sum(len(v) for v in lines.values())} lines, "
          f"{len(classes)} classes, {len(files)} files, {len(intern)} unique sets, {len(cases)} case-split tests, "
          f"{sum(len(v) for v in order_dep.values())} order-dependent lines, {len(data['cost'])} timings, "
          f"{time.monotonic() - T0:.0f}s; "
          f"failed under tracing: {len(bad)} {bad[:5]}; missing {len(missing)} {missing[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
