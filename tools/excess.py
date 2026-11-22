"""Per-commit simulated excess over the oracle. usage: excess.py <dir> <output.jsonl>"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br.schedule import best_split
d, out = sys.argv[1:3]
ts = json.load(open(d + "/tests.json"))["tests"]; a = json.load(open(d + "/answers.json"))["commits"]
G = {i for i, t in enumerate(ts) if t["seconds"] >= 3}
rows = []; gfp = 0
for l in open(out):
    r = json.loads(l); sel = {i for w in r["workers"] for i in w}; f = set(a[r["commit"]]["failing"])
    c = best_split([(round(ts[i]["seconds"] * 1000), i) for i in sel])[0]
    o = best_split([(round(ts[i]["seconds"] * 1000), i) for i in f])[0]
    fp_g = [ts[i]["id"].split("::")[-1] for i in (sel & G) - f]
    gfp += len(fp_g)
    rows.append(((c - o) / 1000, r["commit"], len(sel), len(f), fp_g))
rows.sort(reverse=True)
print("total excess", round(sum(r[0] for r in rows), 2), "giant false positives", gfp)
for r in rows[:15]:
    if r[0] > 0: print(r)
