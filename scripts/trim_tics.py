"""Trim a clerk verbal tic the generator overuses: turns opening with "Ha," or "Ha!".

Deterministic: keeps the opener in roughly one call in eight (by uuid hash) and
drops it elsewhere, recapitalizing what follows. Run after generate.py and
before build_vcons.py. Rerunning is a no-op.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIC = re.compile(r"^(?:Ha|Hah|Ha ha)[,!.]\s+")


def keep(uuid):
    return int(hashlib.sha256(uuid.encode()).hexdigest(), 16) % 8 == 0


def main():
    changed = calls = 0
    for f in sorted((ROOT / "build" / "transcripts").glob("*.json")):
        t = json.loads(f.read_text())
        if keep(t["uuid"]):
            continue
        hit = False
        for turn in t["turns"]:
            if turn["speaker"] == "clerk" and TIC.match(turn["text"]):
                rest = TIC.sub("", turn["text"], count=1)
                turn["text"] = rest[:1].upper() + rest[1:]
                changed += 1
                hit = True
        if hit:
            calls += 1
            f.write_text(json.dumps(t, indent=1, ensure_ascii=False))
    print(f"trimmed {changed} clerk openers in {calls} calls")


if __name__ == "__main__":
    main()
