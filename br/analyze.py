"""Turn a patch into change "sites" and candidate test masks.

A site is one minimal AST difference between the pristine and patched file.
Each site gets a mask of tests that can reach it:
  * runtime code (inside a function/lambda body): tests whose coverage hit the
    enclosing statement (or neighbouring statements for an insertion).
  * import-time code inside a class body: tests that instantiated the class or
    a subclass, plus tests that ran any of the class's methods.
  * module-level code: tests that ran any line in the module, instantiated any
    class from it, or used the dialect the module belongs to.
  * non-Python files: tests that opened/listed the file.
"""

from __future__ import annotations

import ast
import os

from . import diffapply

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DIALECT_DIRS = ("dialects", "parsers", "generators", "typing", "tokenizers")


class Site:
    __slots__ = ("file", "kind", "mask", "strong", "desc", "detail", "probes")

    def __init__(self, file, kind, mask, desc="", strong=None, detail=None):
        # detail drives per-fixture-case filtering:
        #   ("lines", rel, lines) | ("class", rel, qualname, lines) | ("fixture", name, new_idx) | None
        self.detail = detail
        self.probes = frozenset()  # module rels whose import must be checked
        self.file = file
        self.kind = kind  # runtime | class | module | function_def | file | broken | unknown
        self.mask = mask
        # tests that execute code at/around the change (vs. merely use the class/module)
        self.strong = mask if strong is None else strong
        self.desc = desc

    def __repr__(self):
        return f"Site({self.file}, {self.kind}, n={bin(self.mask).count('1')}, {self.desc})"


def _fields(node):
    for name, value in ast.iter_fields(node):
        if name in ("ctx", "type_comment"):
            continue
        yield name, value


def _same(x, y):
    if isinstance(x, ast.AST) and isinstance(y, ast.AST):
        return ast.dump(x) == ast.dump(y)
    return type(x) is type(y) and x == y


def ast_diff(a, b, parents, out):
    """Collect (old_node, parent_chain, field) for minimal differences."""
    if type(a) is not type(b):
        out.append((a, parents, ("replace", b)))
        return
    if not isinstance(a, ast.AST):
        if a != b:
            out.append((None, parents, None))
        return
    chain = parents + [a]
    for (name, va), (_, vb) in zip(_fields(a), _fields(b)):
        if isinstance(va, list):
            if len(va) != len(vb):
                # find common prefix/suffix and report differing middle
                i = 0
                lim = min(len(va), len(vb))
                while i < lim and _same(va[i], vb[i]):
                    i += 1
                j = 0
                while j < lim - i and _same(va[-1 - j], vb[-1 - j]):
                    j += 1
                mid_old = va[i:len(va) - j]
                if mid_old:
                    for x in mid_old:
                        out.append((x, chain, name))
                else:
                    # pure insertion into a[name] between i-1 and i
                    neighbours = [x for x in (va[i - 1] if i > 0 else None, va[i] if i < len(va) else None) if x is not None]
                    out.append(("insert", chain, (name, neighbours)))
            else:
                for x, y in zip(va, vb):
                    if isinstance(x, ast.AST) or isinstance(y, ast.AST):
                        ast_diff(x, y, chain, out)
                    elif not _same(x, y):
                        out.append((a, parents, name))
        elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            if va is None or vb is None:
                out.append((a, parents, name))
            else:
                ast_diff(va, vb, chain, out)
        elif not _same(va, vb):
            out.append((a, parents, name))


def _stmt_header_lines(stmt):
    """Lines of a statement excluding nested statement bodies."""
    start = stmt.lineno
    end = getattr(stmt, "end_lineno", start) or start
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        sub = getattr(stmt, field, None)
        if isinstance(sub, list) and sub and isinstance(sub[0], ast.AST) and hasattr(sub[0], "lineno"):
            end = min(end, sub[0].lineno - 1)
    return range(start, max(start, end) + 1)


def _full_lines(node):
    s = getattr(node, "lineno", None)
    if s is None:
        return range(0)
    return range(s, (getattr(node, "end_lineno", s) or s) + 1)


def _mask_lines(linemap, lines):
    m = 0
    for l in lines:
        v = linemap.get(l)
        if v:
            m |= v
    return m


def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _fixture_items(text):
    """Both loader flavours from tests/helpers.py, as comparable tuples per index."""
    pairs = []
    statements = _filter_comments(text).split(";")
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            pairs.append((statements[i].strip(), statements[i + 1].strip()))
    singles = []
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            singles.append("\n".join(lines))
            lines.clear()
    return pairs, singles


def changed_cases(old, new):
    """Per loader flavour (pairs, singles): (old_count, indices in the new file whose content is new)."""
    out = []
    for o, n in zip(_fixture_items(old), _fixture_items(new)):
        have = {}
        for x in o:
            have[x] = have.get(x, 0) + 1
        ch = set()
        for i, x in enumerate(n):
            if have.get(x, 0) > 0:
                have[x] -= 1
            else:
                ch.add(i)
        out.append((len(o), frozenset(ch)))
    return tuple(out)


def module_name(rel):
    return rel[:-3].replace("/", ".").removesuffix(".__init__")


class Analyzer:
    def __init__(self, maps, tests):
        self.lines = maps["lines"]
        self.classes = maps["classes"]
        self.files = maps["files"]
        self.tests = tests
        self.cases = maps.get("cases", {})
        self.import_lines = maps.get("import_lines", {})
        self.importers = maps.get("importers", {})
        self.importers_top = maps.get("importers_top", {})
        self.order_dep = maps.get("order_dep", {})
        self._cost = maps.get("cost", {})
        self.vprof = maps.get("vprof") or None
        # scale traced timings to recorded-seconds units so the prior stays meaningful
        tot_c = sum(self._cost.values()) or 1.0
        tot_r = sum(t["seconds"] for t in tests) or 1.0
        self.cost_scale = tot_r / tot_c
        self.n = len(tests)
        self.all_mask = (1 << self.n) - 1

        # map-index -> runtime test number (identity when tests.json matches the map)
        ids = maps.get("ids")
        run_ids = [t["id"] for t in tests]
        if ids is None or ids == run_ids:
            self.remap = None
            self.unknown = 0
        else:
            pos = {tid: t["number"] for tid, t in zip(run_ids, tests)}
            self.remap = [pos.get(tid) for tid in ids]
            known = set(ids)
            self.unknown = 0
            for t in tests:
                if t["id"] not in known:
                    self.unknown |= 1 << t["number"]
        # per-module index of class masks
        self.mod_classes = {}
        for key, m in self.classes.items():
            mod, _, q = key.partition(":")
            self.mod_classes.setdefault(mod, []).append((q, m))
        # in the map's index space (masks are converted to runtime numbers at the end)
        self.file_tests = {}
        if self.remap is None:
            for t in tests:
                self.file_tests[t["file"]] = self.file_tests.get(t["file"], 0) | (1 << t["number"])
        else:
            for i, tid in enumerate(ids):
                f = tid.split("::")[0]
                self.file_tests[f] = self.file_tests.get(f, 0) | (1 << i)

    # -- usage masks -------------------------------------------------------
    def class_usage(self, rel, qual, cls_node):
        mod = module_name(rel)
        m = 0
        for q, mm in self.mod_classes.get(mod, ()):
            if q == qual or q.startswith(qual + "."):
                m |= mm
        # methods of the class that ran (classmethods / staticmethods etc.)
        m |= _mask_lines(self.lines.get(rel, {}), _full_lines(cls_node))
        return m

    def import_closure(self, rel):
        """Tests whose file imports `rel` (transitively, at module level): they all
        fail if `rel` cannot be imported."""
        seen = {rel}
        stack = [rel]
        while stack:
            r = stack.pop()
            for imp in self.importers_top.get(r, ()):
                if imp not in seen:
                    seen.add(imp)
                    stack.append(imp)
        m = 0
        for r in seen:
            m |= self.file_tests.get(r, 0)
        return m

    def import_time_modules(self, rel, lines):
        il = self.import_lines.get(rel)
        mods = set()
        if il:
            for l in lines:
                v = il.get(l)
                if v:
                    mods |= v
        return mods

    def import_time_usage(self, rel, lines):
        """Lines executed while some module was being imported affect all of its users."""
        il = self.import_lines.get(rel)
        if not il:
            return 0
        mods = set()
        for l in lines:
            v = il.get(l)
            if v:
                mods |= v
        m = 0
        for mod in mods:
            m |= self.module_usage(mod, importers=False)
        return m

    def class_known(self, rel, qual):
        mod = module_name(rel)
        return any(q == qual or q.startswith(qual + ".") for q, _ in self.mod_classes.get(mod, ()))

    def module_usage(self, rel, importers=True):
        if importers:
            m = self.module_usage(rel, importers=False)
            for imp in self.importers.get(rel, ()):
                m |= self.module_usage(imp, importers=False)
            return m
        m = 0
        for v in self.lines.get(rel, {}).values():
            m |= v
        mod = module_name(rel)
        for q, mm in self.mod_classes.get(mod, ()):
            m |= mm
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS:
            stem = parts[2][:-3]
            for q, mm in self.mod_classes.get(f"sqlglot.dialects.{stem}", ()):
                m |= mm
        if rel.startswith("tests/"):
            m |= self.file_tests.get(rel, 0)
        m |= self.files.get(rel, 0)
        return m

    # -- main ----------------------------------------------------------------
    def analyze_py(self, rel, old_text, new_text):
        linemap = self.lines.get(rel, {})
        sites = []
        try:
            old_tree = ast.parse(old_text) if old_text is not None else None
        except SyntaxError:
            old_tree = None
        if new_text is None or old_text is None or old_tree is None:
            sites.append(Site(rel, "unknown", self.module_usage(rel) | self.files.get(rel, 0), "add/delete"))
            return sites
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            sites.append(Site(rel, "broken", self.module_usage(rel) | self.import_closure(rel), "syntax error"))
            return sites
        diffs = []
        ast_diff(old_tree, new_tree, [], diffs)
        if not diffs:
            # comment / formatting only: only tests that read the source text
            m = self.files.get(rel, 0)
            sites.append(Site(rel, "noop", m, "ast-identical"))
            return sites
        if rel.startswith("tests/"):
            # test files: always also verify the file's own tests (import errors etc.)
            pass
        for node, chain, extra in diffs:
            sites.append(self._classify(rel, linemap, node, chain, extra))
        return sites

    @staticmethod
    def _eval_cmp(op, label):
        """Value of comparison `op` given an observed (identity, equality, order)
        label from the pristine run; None when the observation cannot decide it."""
        i, e, o = label[0] == "1", label[1], label[2]
        if op == "Is":
            return i
        if op == "IsNot":
            return not i
        if op in ("Eq", "NotEq"):
            if e == "u":
                return None
            return (e == "1") if op == "Eq" else (e != "1")
        if o == "u":
            return None
        return {"Lt": o == "l", "LtE": o in "le", "Gt": o == "g", "GtE": o in "ge"}.get(op)

    def _infection_mask(self, rel, node, chain, extra):
        """A comparison whose operator changed can only alter tests whose pristine
        evaluations of it would come out differently under the new operator
        (execution is identical up to the first divergent evaluation)."""
        if self.vprof is None or not isinstance(node, ast.cmpop):
            return None
        if not (extra and extra[0] == "replace" and chain and isinstance(chain[-1], ast.Compare)):
            return None
        cmp = chain[-1]
        if len(cmp.ops) != 1:
            return None
        key = (rel, cmp.lineno, cmp.col_offset)
        if key not in self.vprof["nodes"]:
            return None
        old, new = type(node).__name__, type(extra[1]).__name__
        m = 0
        for label, mask in self.vprof["obs"].get(key, {}).items():
            a, b = self._eval_cmp(old, label), self._eval_cmp(new, label)
            if a is None or b is None or a != b:
                m |= mask
        return m

    def _is_docstring(self, node, chain):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip()):
            return False
        if len(chain) < 2 or not isinstance(chain[-1], ast.Expr):
            return False
        owner = chain[-2]
        body = getattr(owner, "body", None)
        return (
            isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(body, list) and body and body[0] is chain[-1]
        )

    def _classify(self, rel, linemap, node, chain, extra):
        if self._is_docstring(node, chain):
            # docstrings are only observable via __doc__ / source readers
            return Site(rel, "noop", self.files.get(rel, 0), "docstring")
        # Find innermost function/lambda ancestor such that the change is in its body.
        is_insert = node == "insert"
        full = chain + ([node] if isinstance(node, ast.AST) else [])
        func_idx = None
        for i in range(len(full) - 1, -1, -1):
            f = full[i]
            if isinstance(f, FUNC):
                # is the change inside the body (not args/decorators/returns)?
                nxt = full[i + 1] if i + 1 < len(full) else None
                if nxt is None:
                    # the function node itself differs (e.g. name/args field)
                    in_body = is_insert and extra and extra[0] == "body"
                else:
                    if isinstance(f, ast.Lambda):
                        in_body = nxt is f.body
                    else:
                        in_body = any(nxt is s for s in f.body)
                if in_body:
                    func_idx = i
                break
        if func_idx is not None:
            f = full[func_idx]
            # innermost statement inside the function containing the change
            stmt = None
            for x in full[func_idx + 1:]:
                if isinstance(x, ast.stmt):
                    stmt = x
            lines = set()
            if is_insert:
                owner = chain[-1]
                field, neighbours = extra
                for nb in neighbours:
                    if isinstance(nb, ast.stmt):
                        lines.update(_stmt_header_lines(nb))
                    else:
                        lines.update(_full_lines(nb))
                if isinstance(owner, ast.stmt) and owner is not f:
                    lines.update(_stmt_header_lines(owner))
                if not neighbours:
                    lines.update(_full_lines(owner))
            elif stmt is not None:
                lines.update(_stmt_header_lines(stmt))
                if isinstance(node, ast.AST) and hasattr(node, "lineno"):
                    lines.update(_full_lines(node))
            else:
                # lambda body expression
                lines.update(_full_lines(node if isinstance(node, ast.AST) and hasattr(node, "lineno") else f))
            m = _mask_lines(linemap, lines)
            od = self.order_dep.get(rel)
            if od and any(l in od for l in lines):
                # run-once state (cache / lazy load): coverage only credits whichever
                # test ran first, so widen to everything using this module
                site = Site(rel, "runtime", m | self.module_usage(rel), f"{getattr(f, 'name', 'lambda')} +order-dependent")
                site.probes = frozenset({rel} | self.import_time_modules(rel, lines))
                return site
            imp = self.import_time_usage(rel, lines)
            if not imp:
                red = self._infection_mask(rel, node, chain, extra)
                if red is not None:
                    site = Site(rel, "runtime", m & red, f"{getattr(f, 'name', 'lambda')} cmp-swap",
                                detail=("lines", rel, frozenset(lines)))
                    return site
            if imp:
                desc_extra = " +import-time"
                m |= imp
                site = Site(rel, "runtime", m, f"{getattr(f, 'name', 'lambda')}{desc_extra}", detail=None)
                site.probes = frozenset({rel} | self.import_time_modules(rel, lines))
                return site
            if isinstance(f, ast.Lambda):
                # lambda line coverage only proves the lambda ran: same line also
                # holds import-time code, which is not in the map; fine.
                pass
            desc = f"{getattr(f, 'name', 'lambda')}@{min(lines) if lines else '?'}"
            return Site(rel, "runtime", m, desc, detail=("lines", rel, frozenset(lines)))
        # import-time: decorator / signature / class body / module level
        # nearest enclosing def (signature change) -> users of that function
        m = 0
        desc = []
        for i in range(len(full) - 1, -1, -1):
            f = full[i]
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m |= _mask_lines(linemap, _full_lines(f))
                m |= self.import_time_usage(rel, _full_lines(f))
                desc.append(f"def {f.name}")
                break
            if isinstance(f, ast.Lambda):
                m |= _mask_lines(linemap, _full_lines(f))
        strong = m
        detail = None
        cls_chain = [x for x in full if isinstance(x, ast.ClassDef)]
        if cls_chain and not desc:
            detail = ("class", rel, ".".join(c.name for c in cls_chain), frozenset(_full_lines(cls_chain[-1])))
        if cls_chain:
            qual = ".".join(c.name for c in cls_chain)
            m |= self.class_usage(rel, qual, cls_chain[-1])
            if not self.class_known(rel, qual):
                # never instantiated through a wrapped __init__ (enums, namespaces,
                # test classes): fall back to everything that uses the module
                m |= self.module_usage(rel)
            desc.append(f"class {qual}")
            kind = "class"
        else:
            if not desc:
                m |= self.module_usage(rel)
            kind = "module" if not desc else "function_def"
            desc.append("module")
        site = Site(rel, kind, m, " / ".join(desc), strong, detail)
        probes = {rel}
        for i in range(len(full) - 1, -1, -1):
            if isinstance(full[i], (ast.FunctionDef, ast.AsyncFunctionDef)):
                probes |= self.import_time_modules(rel, _full_lines(full[i]))
                break
        site.probes = frozenset(probes)
        return site

    def cost(self, t, recorded):
        """Relative CPU cost of runtime test t (falls back to recorded seconds)."""
        if self._cost:
            i = t if self.remap is None else getattr(self, "_inv", None) and self._inv.get(t)
            if self.remap is not None and not hasattr(self, "_inv"):
                self._inv = {r: j for j, r in enumerate(self.remap) if r is not None}
                i = self._inv.get(t)
            c = self._cost.get(i)
            if c is not None:
                return c * self.cost_scale
        return recorded

    def cases_for(self, t):
        """Per-case map for runtime test number t (None if not case-split)."""
        if self.remap is None:
            return self.cases.get(t)
        if not hasattr(self, "_inv"):
            self._inv = {r: i for i, r in enumerate(self.remap) if r is not None}
        i = self._inv.get(t)
        return None if i is None else self.cases.get(i)

    def _convert(self, m):
        if self.remap is None:
            return m
        out = 0
        i = 0
        while m:
            if m & 1:
                r = self.remap[i]
                if r is not None:
                    out |= 1 << r
            m >>= 1
            i += 1
        return out

    def analyze(self, files: dict):
        sites = self._analyze(files)
        if self.remap is not None:
            for s in sites:
                s.mask = self._convert(s.mask) | self.unknown
                s.strong = self._convert(s.strong) | self.unknown
        return sites

    def _analyze(self, files: dict):
        """files: {rel: (old_text, new_text)} -> list of Site"""
        sites = []
        for rel, (old, new) in files.items():
            if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                sites.extend(self.analyze_py(rel, old, new))
            else:
                m = self.files.get(rel, 0)
                # new file in a listed directory
                d = os.path.dirname(rel)
                while d:
                    m |= self.files.get(d, 0) if old is None else 0
                    d = os.path.dirname(d)
                detail = None
                if rel.startswith("tests/fixtures/") and old is not None and new is not None:
                    detail = ("fixture", rel[len("tests/fixtures/"):], changed_cases(old, new))
                sites.append(Site(rel, "file", m, "data file", detail=detail))
        return sites
