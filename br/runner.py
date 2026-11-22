"""Run sqlglot unittest ids in a forked child, streaming per-test outcomes.

The child imports sqlglot/tests from `root` (an overlay directory), runs the
requested tests with plain unittest, and writes one line per finished test to a
pipe: "<idx>\t<P|F>\t<seconds>". The parent can kill the child at any time.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import time


class _Timeout(BaseException):
    pass


def _test_name(test_id: str) -> tuple[str, str, str]:
    path, cls, meth = test_id.split("::")
    mod = path[:-3].replace("/", ".")
    return mod, cls, meth


def _sync_executor():
    """In-process stand-in for spawn pools; keeps the pickle round trip."""
    import concurrent.futures as cf
    import pickle as pk

    class SyncExecutor:
        def __init__(self, *a, initializer=None, initargs=(), **kw):
            if initializer is not None:
                init, args = pk.loads(pk.dumps((initializer, initargs)))
                init(*args)

        def submit(self, fn, *args, **kwargs):
            f = cf.Future()
            try:
                fn2, args2, kwargs2 = pk.loads(pk.dumps((fn, args, kwargs)))
                r = fn2(*args2, **kwargs2)
                f.set_result(pk.loads(pk.dumps(r)))
            except BaseException as e:  # noqa
                if isinstance(e, _Timeout):
                    raise
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


def _child(root: str, ids: list[tuple[int, str, float]], wfd: int, stop_on_fail: bool, probes=()):
    # Never write to the real stdout.
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    import types
    import unittest

    # spawn-based pools (TestOptimizer) must not re-import our entry script
    sys.modules["__main__"] = types.ModuleType("__main__")
    sys.argv = ["python"]

    out = os.fdopen(wfd, "w", buffering=1)
    sync = _sync_executor() if os.environ.get("BR_SYNC_POOL", "1") == "1" else None

    def report(idx, ok, secs):
        out.write(f"{idx}\t{'P' if ok else 'F'}\t{secs:.4f}\n")
        out.flush()

    def on_alarm(signum, frame):
        raise _Timeout()

    signal.signal(signal.SIGALRM, on_alarm)

    # group by module/class preserving order of first appearance
    loader = unittest.TestLoader()
    mods = {}
    order = []
    specs = {}
    for entry in ids:
        idx, tid, limit = entry[:3]
        specs[idx] = entry[3] if len(entry) > 3 else None
        mod, cls, meth = _test_name(tid)
        key = (mod, cls)
        if key not in mods:
            mods[key] = []
            order.append(key)
        mods[key].append((idx, meth, limit))

    # per-test fixture-case filter: {filename: set(indices)}; files absent -> all cases
    FILTER = [None]

    def wrap_loader(orig):
        if getattr(orig, "_br_wrapped", False):
            return orig

        def loader(filename, *a, **kw):
            spec = FILTER[0]
            allowed = None if spec is None else spec.get(filename)
            for i, item in enumerate(orig(filename, *a, **kw)):
                if allowed is None or i in allowed:
                    yield item

        loader._br_wrapped = True
        return loader

    failed_modules = set()

    # import probes: a module that cannot be imported breaks every importer
    for rel in probes:
        modname = rel[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[: -len(".__init__")]
        try:
            signal.setitimer(signal.ITIMER_REAL, 20)
            __import__(modname)
        except BaseException:
            out.write(f"I\t{rel}\t0\n")
            out.flush()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.bad = False

        def addError(self, test, err):
            if isinstance(err[1], _Timeout):
                pass
            self.bad = True

        def addFailure(self, test, err):
            self.bad = True

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.bad = True

        def addUnexpectedSuccess(self, test):
            self.bad = True

    t_start = time.perf_counter()
    first = True
    for mod, cls in order:
        entries = mods[(mod, cls)]
        try:
            if mod in failed_modules:
                raise ImportError(mod)
            __import__(mod)
            m = sys.modules[mod]
            if sync is not None and hasattr(m, "ProcessPoolExecutor"):
                m.ProcessPoolExecutor = sync
            for lname in ("load_sql_fixture_pairs", "load_sql_fixtures"):
                if callable(getattr(m, lname, None)):
                    setattr(m, lname, wrap_loader(getattr(m, lname)))
            klass = getattr(m, cls)
        except BaseException:
            failed_modules.add(mod)
            for idx, meth, limit in entries:
                report(idx, False, 0.0)
                if stop_on_fail:
                    return
            continue
        # setUpClass once per class
        setup_ok = True
        try:
            signal.setitimer(signal.ITIMER_REAL, max(e[2] for e in entries) + 5)
            unittest.case._Outcome  # noqa
            FILTER[0] = None
            if hasattr(klass, "setUpClass"):
                klass.setUpClass()
        except BaseException:
            setup_ok = False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        for idx, meth, limit in entries:
            if not setup_ok:
                report(idx, False, 0.0)
                if stop_on_fail:
                    return
                continue
            t0 = time.perf_counter()
            FILTER[0] = specs.get(idx)
            res = Result()
            try:
                test = klass(meth)
                signal.setitimer(signal.ITIMER_REAL, limit)
                test.run(res)
            except BaseException:
                res.bad = True
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            ok = not res.bad and not res.errors and not res.failures
            report(idx, ok, time.perf_counter() - t0)
            if not ok and stop_on_fail:
                return
        try:
            if hasattr(klass, "tearDownClass"):
                klass.tearDownClass()
        except BaseException:
            pass
        try:
            klass.doClassCleanups()
        except BaseException:
            pass
    out.close()


def run(root: str, ids, stop_on_fail=False, deadline=None, on_result=None, preexec=None, probes=(), on_probe=None):
    """ids: list of (idx, test_id, timeout_seconds). Returns {idx: ok_bool}.

    Tests not reported (child died / killed at deadline) are absent.
    If the child dies mid-test, that test is reported as failed.
    """
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setpgid(0, 0)
            os.close(rfd)
            if preexec:
                preexec()
            _child(root, ids, wfd, stop_on_fail, probes)
        except BaseException:
            pass
        finally:
            os._exit(0)
    os.close(wfd)
    results = {}
    buf = b""
    order = [e[0] for e in ids]
    limits = {e[0]: e[2] for e in ids}
    last = time.monotonic()
    killed = False
    stopped = False  # caller asked us to stop: no synthetic failure for the next test
    eof = False
    while not eof:
        now = time.monotonic()
        pending = [i for i in order if i not in results]
        cur_limit = limits[pending[0]] + 10 if pending else 10
        wait = cur_limit - (now - last)
        if deadline is not None:
            wait = min(wait, deadline - now)
        if wait <= 0:
            killed = True
            break
        r, _, _ = select.select([rfd], [], [], wait)
        if not r:
            continue
        chunk = os.read(rfd, 65536)
        if not chunk:
            eof = True
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            idx, st, secs = line.decode().split("\t")
            if idx == "I":
                if on_probe:
                    on_probe(st)
                last = time.monotonic()
                continue
            idx = int(idx)
            results[idx] = st == "P"
            last = time.monotonic()
            stop = on_result(idx, st == "P", float(secs)) if on_result else False
            if stop or (stop_on_fail and st != "P"):
                stopped = bool(stop)
                killed = True
                eof = True
                break
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    os.close(rfd)
    os.waitpid(pid, 0)
    if not killed and not stop_on_fail:
        # child exited normally or crashed: first unreported test failed
        pending = [i for i in order if i not in results]
        if pending:
            results[pending[0]] = False
    elif killed and not stopped and (deadline is None or time.monotonic() < deadline):
        pending = [i for i in order if i not in results]
        if pending and not (stop_on_fail and any(not v for v in results.values())):
            results[pending[0]] = False  # hung test = timeout = fail
    return results
