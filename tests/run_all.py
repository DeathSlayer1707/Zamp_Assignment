"""Run every sample through the pipeline in demo order and check the decision."""
import json, os, shutil, sys
from pathlib import Path
os.environ["STAGE_DELAY_MS"] = "0"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STORAGE_DIR", "/tmp/ap-test-storage")
shutil.rmtree(os.environ["STORAGE_DIR"], ignore_errors=True)
from app import db, pipeline
db.init(reset=True)
manifest = json.loads((ROOT / "samples/manifest.json").read_text())
order = [m["file"] for m in manifest]
ok = True
def go(fname, expect):
    global ok
    rid = db.create_run(fname, None, str(ROOT / "samples" / fname))
    pipeline.run(rid, str(ROOT / "samples" / fname), fname)
    r = db.q("SELECT * FROM runs WHERE id=?", (rid,))[0]
    flag = "OK " if r["decision"] == expect else "XX "
    ok &= r["decision"] == expect
    print(f"{flag}{fname:45s} {r['decision']:13s} [{r['queue']}] conf={r['confidence']}")
    res = json.loads(r["result_json"] or "{}")
    for st, v in res.get("stages", {}).items():
        for f in v["findings"]:
            if f["severity"] in ("warn", "fail", "block"):
                print(f"      {st:10s} {f['severity']:5s} {f['code']}: {f['title']}")
    print("      ->", r["summary"][:200])
for m in manifest:
    go(m["file"], m["expect"])
# re-send the happy path file: exact duplicate
go("01_acme_happy_path.pdf", "rejected")
print("ALL PASS" if ok else "MISMATCHES")
