"""Gate: run the full suite through our harness on each sample patch and compare
with sample/answers.json. Usage: python tools/gate.py <sqlglot_root> <sample_dir> <workdir> [ids...]"""

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import overlay, runner  # noqa: E402

BASE, SAMPLE, WORK = sys.argv[1], sys.argv[2], sys.argv[3]
ONLY = sys.argv[4:]
tests = json.load(open(os.path.join(SAMPLE, "tests.json")))["tests"]
answers = json.load(open(os.path.join(SAMPLE, "answers.json")))["commits"]


def one(cid):
    if cid == "__base__":
        root = BASE
    else:
        text = open(os.path.join(SAMPLE, "commits", cid + ".patch")).read()
        pf, _ = overlay.patched_files(BASE, text)
        root = overlay.build(BASE, os.path.join(WORK, cid), {k: v[1] for k, v in pf.items()})
    ids = [(t["number"], t["id"], max(30.0, 10 * t["seconds"])) for t in tests]
    res = runner.run(root, ids)
    failing = sorted(i for i, ok in res.items() if not ok)
    missing = [i for i in range(len(tests)) if i not in res]
    return cid, failing, missing


if __name__ == "__main__":
    cids = ONLY or ["__base__"] + sorted(answers)
    bad = 0
    with ProcessPoolExecutor(int(os.environ.get("J", "10"))) as ex:
        for cid, failing, missing in ex.map(one, cids):
            want = sorted(answers.get(cid, {"failing": []})["failing"])
            ok = failing == want
            bad += not ok
            extra = [tests[i]["id"] for i in set(failing) - set(want)]
            miss = [tests[i]["id"] for i in set(want) - set(failing)]
            print(cid, "OK" if ok else "DIFF", len(failing), len(want), "extra", extra[:5], "miss", miss[:5], "unreported", len(missing), flush=True)
    print("mismatches", bad)
