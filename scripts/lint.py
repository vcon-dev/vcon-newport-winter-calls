#!/usr/bin/env python3
"""Deterministic checks over vcons/ against truth/manifest.json.

Usage: lint.py [--skip-dupes] [--partial]
  --skip-dupes  skip the near-duplicate transcript check
  --partial     do not fail when fewer vCons exist than total_calls (only checks files present)
Exit status is non-zero when any check fails.
"""
import collections
import json
import re
import sys
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config" / "corpus.yaml").read_text())
MANIFEST = json.loads((ROOT / "truth" / "manifest.json").read_text())
TEST_CARDS = {c.replace(" ", "") for c in CFG["test_cards"]}
DEPTS = set(CFG["departments"])
CLUSTER_STREETS = sorted({a["street"] for a in CFG["cluster"]["addresses"]})
STORM = {s["id"]: s for s in CFG["storms"]}
LAWFUL = {"consent", "contract", "legal_obligation", "vital_interests", "public_task", "legitimate_interests"}
GRANT_PURPOSES = ["recording", "transcription", "analysis", "redistribution"]

LAWYER_RE = re.compile(r"\blawyer\w*", re.I)
CARD_RE = re.compile(r"(?<!\d)(?:\d[\s,.\-]{0,4}){12,18}\d(?!\d)")
NOISE_RE = re.compile(r"\[[^\]]*\]|\.{2,}|\u2026")
DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
DIGIT_WORD_RE = re.compile(r"\b(" + "|".join(DIGIT_WORDS) + r")\b", re.I)


def numeralize(text):
    """Spoken digit words become digits so 'four one one one ...' is caught like '4111 ...'."""
    text = NOISE_RE.sub(" ", text)
    return DIGIT_WORD_RE.sub(lambda m: DIGIT_WORDS[m.group(1).lower()], text)
TEST_CARDS = [re.sub(r"\D", "", c) for c in yaml.safe_load(open(ROOT / "config" / "corpus.yaml"))["test_cards"]]
PHONE_RE = re.compile(r"555\s*01\d\d")
# phone-call boilerplate that is expected to repeat
COMMON_RE = re.compile(r"how can i help|can i help you|this is|let me|thank you|give me a|one second|have a good|take care|you're welcome|is there anything|what's the address|can i get|let me pull|pull that up|i can help")
FLOOD_RE = re.compile(r"flood|drain|basement|cellar|catch basin|backed up|sump", re.I)
SHINGLE = 5
DF_CAP = 60

failures = []
warnings = []


def fail(msg):
    failures.append(msg)


def warn(msg):
    warnings.append(msg)


def luhn(num):
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def aware(ts):
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def check_shape(name, v):
    errs = []
    for k in ("vcon", "uuid", "created_at", "subject", "parties", "dialog", "attachments", "extensions"):
        if k not in v:
            errs.append(f"missing {k}")
    if errs:
        return errs, None
    if v["vcon"] != "0.4.0":
        errs.append(f"vcon version {v['vcon']!r}")
    if "analysis" in v:
        errs.append("has analysis key")
    if v["extensions"] != ["lawful_basis"] and "lawful_basis" not in v["extensions"]:
        errs.append("extensions lacks lawful_basis")
    if aware(v["created_at"]) is None:
        errs.append("created_at not ISO with offset")
    if not str(v["subject"]).startswith("Inbound call, "):
        errs.append("subject format")
    p = v["parties"]
    if len(p) != 2 or p[0].get("role") != "resident" or p[1].get("role") != "clerk":
        errs.append("parties roles")
    else:
        for x in p:
            if x.get("validation") != "synthetic" or not x.get("name"):
                errs.append("party name/validation")
            if set(x) & {"tel", "mailto", "email", "phone", "address"}:
                errs.append("party carries contact data")
    total = 0
    prev_end = None
    for i, d in enumerate(v["dialog"]):
        for k in ("type", "start", "duration", "parties", "originator", "mediatype", "encoding", "body"):
            if k not in d:
                errs.append(f"dialog[{i}] missing {k}")
        if errs:
            break
        if d["type"] != "text" or d["mediatype"] != "text/plain" or d["encoding"] != "none":
            errs.append(f"dialog[{i}] type/mediatype/encoding")
        st = aware(d["start"])
        if st is None:
            errs.append(f"dialog[{i}] start not ISO with offset")
        elif prev_end is not None and st != prev_end:
            errs.append(f"dialog[{i}] not contiguous")
        if not isinstance(d["duration"], int) or d["duration"] < 1:
            errs.append(f"dialog[{i}] duration")
        else:
            total += d["duration"]
            if st is not None:
                prev_end = st + timedelta(seconds=d["duration"])
        if d["parties"] != [d["originator"]] or d["originator"] not in (0, 1):
            errs.append(f"dialog[{i}] parties/originator")
        if not isinstance(d["body"], str) or not d["body"].strip():
            errs.append(f"dialog[{i}] empty body")
    if not v["dialog"]:
        errs.append("no dialog")
    atts = {}
    first_start = v["dialog"][0]["start"] if v["dialog"] else None
    for i, a in enumerate(v["attachments"]):
        for k in ("purpose", "party", "dialog", "start", "mediatype", "encoding", "body"):
            if k not in a:
                errs.append(f"attachments[{i}] missing {k}")
        if a.get("mediatype") != "application/json" or a.get("encoding") != "json" or not isinstance(a.get("body"), dict):
            errs.append(f"attachments[{i}] mediatype/encoding/raw JSON body")
        if aware(a.get("start")) is None:
            errs.append(f"attachments[{i}] start")
        elif first_start and a["start"] != first_start:
            errs.append(f"attachments[{i}] start differs from call start")
        atts[a.get("purpose")] = a
    if sorted(k for k in atts if k) != ["call_detail", "lawful_basis", "tags"] or len(v["attachments"]) != 3:
        errs.append(f"attachment purposes {sorted(map(str, atts))}")
        return errs, None
    tags = atts["tags"]["body"]
    if set(tags) != {"department"} or tags.get("department") not in DEPTS:
        errs.append(f"tags body {tags}")
    cd = atts["call_detail"]["body"]
    for k in ("direction", "start", "end", "duration_seconds", "channel"):
        if k not in cd:
            errs.append(f"call_detail missing {k}")
    if "duration_seconds" in cd:
        dur = cd["duration_seconds"]
        if not (60 <= dur <= 720):
            errs.append(f"duration {dur} outside 60-720")
        if dur != total:
            errs.append(f"dialog durations sum {total} != call_detail {dur}")
        if aware(cd.get("start")) is None or aware(cd.get("end")) is None:
            errs.append("call_detail times")
        elif aware(cd["end"]) - aware(cd["start"]) != timedelta(seconds=dur) or cd["end"] != v["created_at"]:
            errs.append("call_detail end / created_at mismatch")
        if cd.get("direction") != "inbound" or cd.get("channel") not in ("landline", "cell", "cell-poor", "speakerphone"):
            errs.append("call_detail direction/channel")
    lb = atts["lawful_basis"]["body"]
    if lb.get("lawful_basis") not in LAWFUL:
        errs.append("lawful_basis value")
    if "expiration" not in lb:
        errs.append("lawful_basis expiration key")
    pg = lb.get("purpose_grants")
    if not isinstance(pg, list) or [g.get("purpose") for g in pg] != GRANT_PURPOSES or not all(g.get("granted") is True and aware(g.get("granted_at")) for g in pg):
        errs.append("lawful_basis purpose_grants")
    pm = lb.get("proof_mechanisms")
    if not isinstance(pm, list) or not pm or not all(m.get("mechanism_type") and m.get("description") for m in pm):
        errs.append("lawful_basis proof_mechanisms")
    start = aware(cd.get("start")) if "start" in cd else None
    if start is not None:
        exp = f"{start:%Y-%m-%d}-{start:%H%M}-{tags.get('department')}-{v['uuid'][:8]}.vcon.json"
        if name != exp:
            errs.append(f"filename should be {exp}")
    return errs, start


def shingles(text):
    toks = re.findall(r"[a-z0-9']+", text.lower())
    return {zlib.crc32(" ".join(toks[i:i + SHINGLE]).encode()) for i in range(max(0, len(toks) - SHINGLE + 1))}


def near_dupes(docs):
    """docs: {uuid: set(shingles)} -> list of (jaccard, a, b) above 0.5."""
    index = collections.defaultdict(list)
    for u, s in docs.items():
        for h in s:
            index[h].append(u)
    pair = collections.Counter()
    for h, us in index.items():
        if 1 < len(us) <= DF_CAP:
            for i in range(len(us)):
                for j in range(i + 1, len(us)):
                    pair[(us[i], us[j])] += 1
    out = []
    for (a, b), c in pair.items():
        sa, sb = docs[a], docs[b]
        if c < 0.2 * min(len(sa), len(sb)):
            continue
        j = len(sa & sb) / len(sa | sb)
        if j > 0.5:
            out.append((j, a, b))
    return sorted(out, reverse=True)


def main():
    skip_dupes = "--skip-dupes" in sys.argv
    partial = "--partial" in sys.argv
    files = sorted((ROOT / "vcons").glob("*.vcon.json"))
    total = MANIFEST["total_calls"]
    F = MANIFEST["findings"]
    lawyer_set = set(F["lawyer"]["uuids"])
    card_set = set(F["card_full_read"]["uuids"])
    cluster_set = set(F["drainage_cluster"]["uuids"])
    by_uuid = {}
    docs = {}
    shape_errors = 0
    lawyer_hits = {}
    lawyer_clerk = []
    luhn_hits = collections.defaultdict(list)
    cluster_warn = []
    cluster_dates = {}
    ayuh = []
    caller_streams = {}
    ngram_df = collections.Counter()
    for f in files:
        try:
            v = json.loads(f.read_text())
        except Exception as e:
            fail(f"{f.name}: invalid JSON ({e})")
            continue
        errs, start = check_shape(f.name, v)
        if errs:
            shape_errors += 1
            for e in errs[:4]:
                fail(f"{f.name}: {e}")
        u = v.get("uuid")
        if u in by_uuid:
            fail(f"duplicate uuid {u}")
        by_uuid[u] = f.name
        if errs and not v.get("dialog"):
            continue
        texts = []
        for d in v.get("dialog", []):
            body = d.get("body", "") if isinstance(d.get("body"), str) else ""
            texts.append(body)
            who = "caller" if d.get("originator") == 0 else "clerk"
            if who == "caller":
                caller_streams[u] = caller_streams.get(u, "") + " " + re.sub(r"\D", "", numeralize(body))
            if LAWYER_RE.search(body):
                if who == "caller":
                    lawyer_hits.setdefault(u, []).append(f.name)
                else:
                    lawyer_clerk.append(f.name)
            for m in CARD_RE.finditer(numeralize(body)):
                digs = re.sub(r"\D", "", m.group())
                if 13 <= len(digs) <= 19 and luhn(digs):
                    luhn_hits[u].append((who, digs))
            if u not in cluster_set and who == "caller":
                for st in CLUSTER_STREETS:
                    if st.lower() in body.lower() and FLOOD_RE.search(body):
                        cluster_warn.append(f"{f.name}: names {st} near flooding words")
        full = " ".join(texts)
        if re.search(r"\bayuh\b", full, re.I):
            ayuh.append(f.name)
        for m in PHONE_RE.finditer(numeralize(full).replace("-", " ")):
            n = int(re.sub(r"\D", "", m.group())[-2:])
            if n < 50 and u not in cluster_set:
                fail(f"cluster household phone 555-01{n:02d} in non-cluster call {f.name}")
        words = re.findall(r"[a-z']+", full.lower())
        for g in {" ".join(words[i:i + 5]) for i in range(len(words) - 4)}:
            ngram_df[g] += 1
        docs[u] = shingles(full)
        if start is not None and u in cluster_set:
            cluster_dates[u] = start.date()

    for n in ayuh:
        fail(f"uses Ayuh: {n}")
    common = [(g, c) for g, c in ngram_df.most_common(400) if c >= 40 and not COMMON_RE.search(g)]
    if common:
        print("  most repeated 5-word phrases (calls):")
        for g, c in common[:15]:
            print(f"    {c:4d}  {g}")

    # coverage
    all_manifest = set()
    for t in MANIFEST["topics"].values():
        all_manifest.update(t["uuids"])
    missing = sorted(all_manifest - set(by_uuid))
    extra = sorted(set(by_uuid) - all_manifest)
    if not partial:
        if missing:
            fail(f"{len(missing)} manifest uuids have no file (first: {missing[0]})")
        if len(files) != total:
            fail(f"file count {len(files)} != total_calls {total}")
    for u in extra:
        fail(f"file for uuid not in manifest: {u}")

    # lawyer
    present_law = set(lawyer_hits)
    for u in lawyer_set:
        if u in by_uuid and u not in present_law:
            fail(f"lawyer uuid {u} lacks the word lawyer in a caller turn")
    for u in present_law - lawyer_set:
        fail(f"word lawyer in non-lawyer call {u} ({lawyer_hits[u][0]})")
    for n in lawyer_clerk:
        fail(f"clerk says lawyer: {n}")
    if not partial and (len(present_law & lawyer_set) != len(lawyer_set)):
        fail("lawyer word coverage incomplete")

    # cards
    for u, hits in luhn_hits.items():
        if u not in card_set:
            fail(f"Luhn-valid card number in non-card call {u} ({by_uuid[u]})")
            continue
        for who, digs in hits:
            if digs not in TEST_CARDS:
                fail(f"card number not a configured test card in {u}")
    for u in card_set:
        if u in by_uuid and u in caller_streams and any(c in caller_streams[u] for c in TEST_CARDS):
            continue
        if u in by_uuid and not any(w == "caller" for w, _ in luhn_hits.get(u, [])):
            fail(f"card_full_read uuid {u} has no Luhn-valid card read by the caller ({by_uuid[u]})")

    # cluster windows
    for u, d in cluster_dates.items():
        if not any(s["end"] <= d <= s["end"] + timedelta(days=3) for s in STORM.values()):
            fail(f"cluster call {u} dated {d} outside every storm end..end+3d window")
    for w in cluster_warn:
        warn(w)

    dupes = []
    if not skip_dupes:
        dupes = near_dupes(docs)
        for j, a, b in dupes[:20]:
            fail(f"near-duplicate transcripts (Jaccard {j:.2f}): {by_uuid[a]} / {by_uuid[b]}")
        if len(dupes) > 20:
            fail(f"... {len(dupes) - 20} more near-duplicate pairs")

    print("lint report")
    print(f"  vcon files {len(files)} of {total}; shape-error files {shape_errors}")
    print(f"  manifest: lawyer {len(lawyer_set)} (found in {len(present_law & lawyer_set)}), card_full_read {len(card_set)} "
          f"(Luhn-valid found in {sum(1 for u in card_set if u in luhn_hits)}), cluster {len(cluster_set)} "
          f"(dated {len(cluster_dates)})")
    print(f"  near-duplicate pairs: {'skipped' if skip_dupes else len(dupes)}")
    print(f"  warnings {len(warnings)}", end="")
    print("" if not warnings else " (first 5):")
    for w in warnings[:5]:
        print("   ", w)
    if failures:
        print(f"FAIL: {len(failures)} problems")
        for m in failures[:40]:
            print("  -", m)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
