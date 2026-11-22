"""Show sites/candidates for sample patches and check recall."""
import json, os, pickle, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import overlay
from br.analyze import Analyzer
BASE, MAPS, SAMPLE = sys.argv[1:4]
tests = json.load(open(f"{SAMPLE}/tests.json"))["tests"]
ans = json.load(open(f"{SAMPLE}/answers.json"))["commits"] if os.path.exists(f"{SAMPLE}/answers.json") else {}
an = Analyzer(pickle.load(open(MAPS, "rb")), tests)
GI = 3.0
tot_c = 0
for f in sorted(os.listdir(f"{SAMPLE}/commits")):
    cid = f[:-6]
    pf, _ = overlay.patched_files(BASE, open(f"{SAMPLE}/commits/{f}").read())
    sites = an.analyze(pf)
    m = 0
    for s in sites: m |= s.mask
    cand = [i for i in range(len(tests)) if m >> i & 1]
    cheap = sum(tests[i]["seconds"] for i in cand if tests[i]["seconds"] < GI)
    giants = [tests[i]["id"].split("::")[-1] for i in cand if tests[i]["seconds"] >= GI]
    fail = set(ans.get(cid, {}).get("failing", []))
    missed = fail - set(cand)
    tot_c += cheap
    print(f"{cid} {ans.get(cid,{}).get('kind','?'):8} fail={len(fail):3} cand={len(cand):4} cheap_s={cheap:6.2f} giants={giants} {'MISSED '+str([tests[i]['id'] for i in missed]) if missed else ''}")
    for s in sites: print("     ", s)
print("total cheap candidate seconds", round(tot_c, 2))
