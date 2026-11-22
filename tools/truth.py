"""Ground truth for synthetic mutants: full suite per patch -> answers.json.
usage: python tools/truth.py <sqlglot_root> <tests.json> <mut_dir> <workdir>"""
import json, os, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import overlay, runner
BASE, TESTS, MUT, WORK = sys.argv[1:5]
MAPS = sys.argv[5] if len(sys.argv) > 5 else None
tests = json.load(open(TESTS))["tests"]
an = None
if MAPS:
    import pickle
    from br.analyze import Analyzer
    an = Analyzer(pickle.load(open(MAPS, "rb")), tests)

def one(cid):
    text = open(os.path.join(MUT, "commits", cid + ".patch")).read()
    pf, _ = overlay.patched_files(BASE, text)
    root = overlay.build(BASE, os.path.join(WORK, cid), {k: v[1] for k, v in pf.items()})
    skip = set()
    if an is not None:
        m = 0
        for site in an.analyze(pf):
            m |= site.mask
        skip = {t["number"] for t in tests if t["seconds"] >= 3.0 and not (m >> t["number"] & 1)}
    ids = [(t["number"], t["id"], max(15.0, 5 * t["seconds"])) for t in tests if t["number"] not in skip]
    res = runner.run(root, ids)
    import shutil; shutil.rmtree(root, ignore_errors=True)
    return cid, sorted(i for i, ok in res.items() if not ok)

if __name__ == "__main__":
    cids = sorted(f[:-6] for f in os.listdir(os.path.join(MUT, "commits")))
    out = {}
    done_path = os.path.join(MUT, "truth.jsonl")
    if os.path.exists(done_path):
        for line in open(done_path):
            d = json.loads(line)
            out[d["cid"]] = {"failing": d["failing"], "kind": "synthetic"}
    cids = [c for c in cids if c not in out]
    done_f = open(done_path, "a")
    with ProcessPoolExecutor(int(os.environ.get("J", "10"))) as ex:
        for cid, failing in ex.map(one, cids):
            out[cid] = {"failing": failing, "kind": "synthetic"}
            done_f.write(json.dumps({"cid": cid, "failing": failing}) + "\n"); done_f.flush()
            print(cid, len(failing), flush=True)
    json.dump({"commits": out}, open(os.path.join(MUT, "answers.json"), "w"))
