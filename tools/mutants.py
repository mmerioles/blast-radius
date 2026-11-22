"""Generate synthetic mutation patches (like the sample) against the pristine tree.

Used only offline to validate recall and calibrate generic policies.
usage: python tools/mutants.py <sqlglot_root> <out_dir> <count> [seed]
Writes <out_dir>/commits/<id>.patch
"""

import ast
import difflib
import os
import random
import re
import sys

ROOT, OUT, COUNT = sys.argv[1], sys.argv[2], int(sys.argv[3])
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 1
rng = random.Random(SEED)

SWAPS = [
    (r" and ", " or "), (r" or ", " and "), (r" is not ", " != "), (r" is None", " == None"),
    (r" is ", " == "), (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "),
    (r" > ", " >= "), (r" >= ", " > "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bnot ", ""), (r" \+ 1\b", " - 1"), (r" - 1\b", " + 1"), (r"\bin\b", "not in"),
    (r"\breturn True\b", "return False"), (r"\bbreak\b", "continue"),
]


def py_files():
    out = []
    for d, _, fs in os.walk(os.path.join(ROOT, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), ROOT))
    return sorted(out)


def fixture_files():
    out = []
    for d, _, fs in os.walk(os.path.join(ROOT, "tests/fixtures/optimizer")):
        for f in fs:
            if f.endswith(".sql") and "tpc" not in d:
                out.append(os.path.relpath(os.path.join(d, f), ROOT))
    return sorted(out)


def mutate_py(text):
    lines = text.split("\n")
    for _ in range(200):
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith('"""') or s.startswith("import") or s.startswith("from "):
            continue
        r = rng.random()
        if r < 0.08:
            indent = line[: len(line) - len(line.lstrip())]
            new = lines[:i] + [indent + "# " + rng.choice(["TODO", "Cheap check first", "Kept for compat", "NOTE: hot path"])] + lines[i:]
        elif r < 0.18:
            # integer constant tweak
            ms = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
            if not ms:
                continue
            m = rng.choice(ms)
            v = int(m.group(1))
            line2 = line[: m.start()] + str(v + rng.choice([1, -1] if v else [1])) + line[m.end():]
            new = lines[:i] + [line2] + lines[i + 1:]
        elif r < 0.25:
            ms = list(re.finditer(r'"([A-Za-z_ ]{2,})"', line))
            if not ms:
                continue
            m = rng.choice(ms)
            line2 = line[: m.start() + 1] + m.group(1)[:-1] + line[m.end() - 1:]
            new = lines[:i] + [line2] + lines[i + 1:]
        else:
            opts = [(p, q) for p, q in SWAPS if re.search(p, line)]
            if not opts:
                continue
            p, q = rng.choice(opts)
            ms = list(re.finditer(p, line))
            m = rng.choice(ms)
            line2 = line[: m.start()] + q + line[m.end():]
            new = lines[:i] + [line2] + lines[i + 1:]
        new_text = "\n".join(new)
        try:
            ast.parse(new_text)
        except SyntaxError:
            continue
        return new_text
    return None


def mutate_sql(text):
    lines = text.split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.startswith("#") or line.startswith("--"):
            continue
        r = rng.random()
        toks = line.split(" ")
        if r < 0.5:
            j = rng.randrange(len(toks) + 1)
            toks = toks[:j] + [rng.choice(["x", "42", "AND", "1"])] + toks[j:]
        else:
            j = rng.randrange(len(toks))
            toks = toks[:j] + toks[j + 1:]
        return "\n".join(lines[:i] + [" ".join(toks)] + lines[i + 1:])
    return None


def main():
    os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
    pys = py_files()
    # weight towards dialect/parser/generator modules like the sample
    fx = fixture_files()
    n = 0
    while n < COUNT:
        if rng.random() < 0.25:
            rel = rng.choice(fx)
            old = open(os.path.join(ROOT, rel)).read()
            new = mutate_sql(old)
        else:
            rel = rng.choice(pys)
            old = open(os.path.join(ROOT, rel)).read()
            new = mutate_py(old)
        if new is None or new == old:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3
        )
        body = "".join(diff)
        if not body:
            continue
        cid = f"m{SEED}x{n:03d}"
        with open(os.path.join(OUT, "commits", cid + ".patch"), "w") as f:
            f.write(f"diff --git a/{rel} b/{rel}\n" + body)
        n += 1


if __name__ == "__main__":
    main()
