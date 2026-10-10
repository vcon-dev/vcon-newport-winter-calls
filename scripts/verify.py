#!/usr/bin/env python3
"""Blind verification of the vCon corpus.

Claude reads each vCon (rendered as department tag, start time and transcript
only; no specs, manifest or filenames) and answers a fixed questionnaire. The
compare step recomputes every planted finding from the answers and diffs it
against truth/manifest.json by uuid. --compare-only makes no API calls."""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_common as lc  # noqa: E402

ROOT = lc.ROOT
PROMPT = "config/prompts/verify.md"
TONES = ["frustrated", "annoyed", "neutral", "confused", "worried", "friendly"]
FINDINGS = ["drainage_cluster", "eleanor_pucci", "repeat_floods", "lawyer",
            "card_full_read", "after_hours_pay", "fence_permit", "downed_tree_where"]


def load_cfg(path=None):
    return yaml.safe_load(Path(path or ROOT / "config/corpus.yaml").read_text())


def schema(topics):
    nullable = lambda t: {"type": [t, "null"]}  # noqa: E731
    props = {
        "topic": {"type": "string", "enum": list(topics)},
        "tone": {"type": "string", "enum": TONES},
        "caller_address": nullable("string"),
        "flooding_report": {"type": "boolean"},
        "basement_flooded_this_call": {"type": "boolean"},
        "basement_flood_count_this_winter": nullable("integer"),
        "says_lawyer": {"type": "boolean"},
        "caller_threatens_legal_action": {"type": "boolean"},
        "full_card_number_read_aloud": {"type": "boolean"},
        "asks_pay_after_hours_or_online": {"type": "boolean"},
        "asks_fence_permit": {"type": "boolean"},
        "asks_where_report_downed_tree": {"type": "boolean"},
        "storm_mentioned": nullable("string"),
        "notes": {"type": "string"},
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ---- vCon rendering --------------------------------------------------------
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def vcon_info(v):
    """Return (department, start_iso, rendered_text, caller_text)."""
    dept = start = None
    for att in v.get("attachments", []):
        p = att.get("purpose") or att.get("type")
        body = att.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                body = {}
        if p == "tags" and isinstance(body, dict):
            dept = body.get("department")
        if p == "call_detail" and isinstance(body, dict):
            start = body.get("start")
    d = v.get("dialog", [])
    start = start or (d[0]["start"] if d else None)
    parties = v.get("parties", [])
    t0 = parse_ts(start) if start else None
    lines, caller = [], []
    for e in d:
        idx = (e.get("parties") or [0])[0]
        p = parties[idx] if idx < len(parties) else {}
        role = "Caller" if p.get("role") == "resident" else "Clerk"
        off = int((parse_ts(e["start"]) - t0).total_seconds()) if t0 and e.get("start") else 0
        off = max(off, 0)
        ts = f"{off // 3600:02d}:{off % 3600 // 60:02d}:{off % 60:02d}"
        body = e.get("body", "")
        lines.append(f"[{ts}] {role} ({p.get('name', '?')}): {body}")
        if role == "Caller":
            caller.append(body)
    text = f"Department: {dept}\nCall start: {start}\n\n" + "\n".join(lines)
    return dept, start, text, "\n".join(caller)


# ---- normalisation and regex cross-checks -----------------------------------
def norm_street(s):
    s = re.sub(r"[^\w ]", "", s.lower()).split()
    sub = {"street": "st", "avenue": "ave", "av": "ave"}
    return " ".join(sub.get(w, w) for w in s)


def norm_addr(addr):
    if not addr:
        return None
    m = re.match(r"\s*(\d+)\s+(.+?)\s*$", addr)
    return (int(m.group(1)), norm_street(m.group(2))) if m else None


def luhn(d):
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
        alt = not alt
    return s % 10 == 0


def regex_card(text):
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text):
        d = re.sub(r"\D", "", m.group())
        if 13 <= len(d) <= 19 and luhn(d):
            return True
    return False


def regex_lawyer(text):
    return bool(re.search(r"\blawyers?\b", text, re.I))


# ---- compare ---------------------------------------------------------------
def compare(cfg, manifest, answers, caller_text):
    """answers: uuid -> {"start": iso, "answer": {...}}. caller_text: uuid -> str (may lack uuids).
    Returns result dict."""
    cluster = {(a["number"], norm_street(a["street"])) for a in cfg["cluster"]["addresses"]}
    ele = next(a for a in cfg["cluster"]["addresses"] if a["id"] == cfg["eleanor"]["address"])
    ele_key = (ele["number"], norm_street(ele["street"]))
    ele_date = cfg["eleanor"]["start"][:10]
    F = manifest["findings"]
    obs = {k: set() for k in FINDINGS}
    flood_by_addr = defaultdict(set)   # cluster addr -> uuids with count >= 2
    for u, rec in answers.items():
        a = rec["answer"]
        key = norm_addr(a.get("caller_address"))
        if a["flooding_report"] and key in cluster:
            obs["drainage_cluster"].add(u)
            if key == ele_key and (rec.get("start") or "")[:10] == ele_date:
                obs["eleanor_pucci"].add(u)
            n = a.get("basement_flood_count_this_winter")
            if n is not None and n >= 2:
                flood_by_addr[key].add(u)
        if a["says_lawyer"]:
            obs["lawyer"].add(u)
        if a["full_card_number_read_aloud"]:
            obs["card_full_read"].add(u)
        if a["asks_pay_after_hours_or_online"]:
            obs["after_hours_pay"].add(u)
        if a["asks_fence_permit"]:
            obs["fence_permit"].add(u)
        if a["asks_where_report_downed_tree"]:
            obs["downed_tree_where"].add(u)
    obs["repeat_floods"] = set().union(*flood_by_addr.values()) if flood_by_addr else set()

    res = {}
    for k in FINDINGS:
        if k == "repeat_floods":
            exp_addr = {}
            for e in F[k]["addresses"]:
                key = norm_addr(e["address"])
                exp_addr[key] = set(e["uuids"])
            obs_addr = set(flood_by_addr)
            missing_addr = set(exp_addr) - obs_addr
            extra_addr = obs_addr - set(exp_addr)
            missing = set().union(*[exp_addr[a] for a in missing_addr]) if missing_addr else set()
            extra = set().union(*[flood_by_addr[a] for a in extra_addr]) if extra_addr else set()
            res[k] = {"expected": len(exp_addr), "observed": len(obs_addr),
                      "missing": sorted(missing), "extra": sorted(extra),
                      "missing_addresses": sorted(f"{n} {s}" for n, s in missing_addr),
                      "extra_addresses": sorted(f"{n} {s}" for n, s in extra_addr),
                      "note": "address-level; uuids listed are those at missing/extra addresses"}
        else:
            exp = set(F[k]["uuids"])
            o = obs[k]
            res[k] = {"expected": len(exp), "observed": len(o),
                      "missing": sorted(exp - o), "extra": sorted(o - exp)}
        res[k]["pass"] = not res[k]["missing"] and not res[k]["extra"]

    # topics and tones
    cnt = Counter(r["answer"]["topic"] for r in answers.values())
    tones = defaultdict(Counter)
    for r in answers.values():
        tones[r["answer"]["topic"]][r["answer"]["tone"]] += 1
    exp_topics = manifest["topics"]
    exp_rank = sorted(exp_topics, key=lambda t: -exp_topics[t]["count"])[:5]
    obs_rank = [t for t, _ in cnt.most_common(5)]
    dom = {t: (tones[t].most_common(1)[0][0] if tones[t] else None) for t in exp_topics}
    tone_ok = all(dom[t] == exp_topics[t].get("dominant_tone") for t in exp_rank)
    drift = {t: {"expected": exp_topics[t]["count"], "observed": cnt.get(t, 0),
                 "drift": cnt.get(t, 0) - exp_topics[t]["count"],
                 "expected_tone": exp_topics[t].get("dominant_tone"), "observed_tone": dom[t]}
             for t in exp_topics}
    res["topics"] = {"expected_top5": exp_rank, "observed_top5": obs_rank,
                     "rank_pass": exp_rank == obs_rank, "tone_pass": tone_ok, "detail": drift,
                     "pass": exp_rank == obs_rank and tone_ok}

    # deterministic cross-check
    rx = {"lawyer": {"claude_only": [], "regex_only": []}, "card": {"claude_only": [], "regex_only": []}}
    for u, text in caller_text.items():
        if u not in answers:
            continue
        a = answers[u]["answer"]
        for name, claude, found in (("lawyer", a["says_lawyer"], regex_lawyer(text)),
                                    ("card", a["full_card_number_read_aloud"], regex_card(text))):
            if claude and not found:
                rx[name]["claude_only"].append(u)
            if found and not claude:
                rx[name]["regex_only"].append(u)
    res["regex_crosscheck"] = rx
    res["answered"] = len(answers)
    return res


def mismatch_uuids(res):
    out = set()
    for k in FINDINGS:
        out |= set(res[k]["missing"]) | set(res[k]["extra"])
    return sorted(out)


def write_report(res, path, model, vcon_total):
    L = ["# Verification report", "",
         f"Date: {datetime.now().astimezone().date()}  Model: {model}  Answers: {res['answered']}"
         + (f" of {vcon_total} vCons" if vcon_total is not None else ""), "",
         "## Planted findings", "", "| finding | expected | observed | missing | extra | result |", "|---|---|---|---|---|---|"]
    for k in FINDINGS:
        r = res[k]
        L.append(f"| {k} | {r['expected']} | {r['observed']} | {len(r['missing'])} | {len(r['extra'])} | {'pass' if r['pass'] else 'FAIL'} |")
    for k in FINDINGS:
        r = res[k]
        if r["pass"]:
            continue
        L += ["", f"### {k}"]
        for key in ("missing_addresses", "extra_addresses"):
            if r.get(key):
                L.append(f"{key}: {', '.join(r[key])}")
        L.append(f"missing: {', '.join(r['missing']) or 'none'}")
        L.append(f"extra: {', '.join(r['extra']) or 'none'}")
    t = res["topics"]
    L += ["", "## Topics", "", f"Top 5 expected: {', '.join(t['expected_top5'])}",
          f"Top 5 observed: {', '.join(t['observed_top5'])}",
          f"Rank order {'pass' if t['rank_pass'] else 'FAIL'}; dominant tones {'pass' if t['tone_pass'] else 'FAIL'}", "",
          "| topic | expected | observed | drift | expected tone | observed tone |", "|---|---|---|---|---|---|"]
    for name, d in sorted(t["detail"].items(), key=lambda kv: -kv[1]["expected"]):
        L.append(f"| {name} | {d['expected']} | {d['observed']} | {d['drift']:+d} | {d['expected_tone']} | {d['observed_tone']} |")
    rx = res["regex_crosscheck"]
    L += ["", "## Regex cross-check", ""]
    for name in ("lawyer", "card"):
        L.append(f"{name}: claude yes / regex no: {', '.join(rx[name]['claude_only']) or 'none'}; "
                 f"regex yes / claude no: {', '.join(rx[name]['regex_only']) or 'none'}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(L) + "\n")


def load_answers(adir):
    out = {}
    for p in sorted(Path(adir).glob("*.json")):
        d = json.loads(p.read_text())
        out[d["uuid"]] = d
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vcons-dir", default=str(ROOT / "vcons"))
    ap.add_argument("--manifest", default=str(ROOT / "truth/manifest.json"))
    ap.add_argument("--config", default=str(ROOT / "config/corpus.yaml"))
    ap.add_argument("--answers-dir", default=str(lc.BUILD / "verify"))
    ap.add_argument("--report", default=str(lc.BUILD / "verify-report.md"))
    ap.add_argument("--summary-out", default=str(ROOT / "truth/verification.json"))
    ap.add_argument("--mismatches-out", help="write uuids to regenerate (missing+extra across planted findings), one per line")
    ap.add_argument("--only", help="comma-separated uuids (implies --force for them)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="re-ask even if an answer exists")
    ap.add_argument("--compare-only", action="store_true", help="no API calls; compare existing answers")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-batch", action="store_true", help="synchronous requests, thread pool of 8")
    ap.add_argument("--resume", action="store_true", help="collect a persisted unfinished batch")
    a = ap.parse_args()

    cfg = load_cfg(a.config)
    adir = Path(a.answers_dir)
    vdir = Path(a.vcons_dir)
    texts, callers, starts = {}, {}, {}
    for p in sorted(vdir.glob("*.vcon.json")):
        v = json.loads(p.read_text())
        _, start, text, caller = vcon_info(v)
        texts[v["uuid"]], callers[v["uuid"]], starts[v["uuid"]] = text, caller, start

    if not a.compare_only:
        import anthropic
        topics = list(cfg["topics"])
        sch = schema(topics)
        system = lc.load_prompt(PROMPT)
        adir.mkdir(parents=True, exist_ok=True)
        want = {x.strip() for x in a.only.split(",")} if a.only else None
        todo = [u for u in texts if (want is None or u in want)
                and (a.force or want or not (adir / f"{u}.json").exists())]
        if a.limit:
            todo = todo[:a.limit]
        if not texts and not a.resume:
            sys.exit(f"no vCons found in {vdir}")
        requests = [(u, lc.make_params(a.model, system, texts[u], sch, a.effort, 4000)) for u in todo]
        print(f"{len(requests)} vCons to verify" if not a.resume else "resuming persisted batch")

        def handle(cid, msg, err):
            if err:
                lc.append_jsonl(lc.BUILD / "verify-errors.jsonl", {"ts": lc.now(), "uuid": cid, "error": err})
                return
            try:
                ans = lc.message_json(msg)
            except (ValueError, KeyError) as ex:
                lc.append_jsonl(lc.BUILD / "verify-errors.jsonl", {"ts": lc.now(), "uuid": cid, "error": f"bad output: {ex}"})
                return
            (adir / f"{cid}.json").write_text(json.dumps(
                {"uuid": cid, "start": starts.get(cid), "answer": ans}, indent=2, ensure_ascii=False))

        client = anthropic.Anthropic(max_retries=5)
        if a.no_batch:
            lc.run_sync(client, "verify", requests, handle)
        else:
            lc.run_batch(client, "verify", requests, handle, a.resume)

    answers = load_answers(adir)
    for u, rec in answers.items():
        rec.setdefault("start", starts.get(u))
    manifest = json.loads(Path(a.manifest).read_text())
    res = compare(cfg, manifest, answers, callers)
    total = len(texts) if texts else None
    write_report(res, a.report, a.model, total)

    planted_ok = all(res[k]["pass"] for k in FINDINGS)
    unanswered = (total - len(answers)) if total is not None else None
    summary = {"date": str(datetime.now().astimezone().date()), "model": a.model,
               "answered": len(answers), "vcons": total, "unanswered": unanswered,
               "findings": {k: {"expected": res[k]["expected"], "observed": res[k]["observed"],
                                "missing": len(res[k]["missing"]), "extra": len(res[k]["extra"]),
                                "pass": res[k]["pass"]} for k in FINDINGS},
               "topics": {"expected_top5": res["topics"]["expected_top5"], "observed_top5": res["topics"]["observed_top5"],
                          "rank_pass": res["topics"]["rank_pass"], "tone_pass": res["topics"]["tone_pass"]},
               "regex_disagreements": {n: len(v["claude_only"]) + len(v["regex_only"]) for n, v in res["regex_crosscheck"].items()},
               "planted_pass": planted_ok}
    Path(a.summary_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.summary_out).write_text(json.dumps(summary, indent=2) + "\n")
    if a.mismatches_out:
        Path(a.mismatches_out).write_text("\n".join(mismatch_uuids(res)) + "\n")

    for k in FINDINGS:
        r = res[k]
        print(f"{'ok  ' if r['pass'] else 'FAIL'} {k}: expected {r['expected']} observed {r['observed']} "
              f"missing {len(r['missing'])} extra {len(r['extra'])}")
    print(f"topics: rank {'ok' if res['topics']['rank_pass'] else 'FAIL'}, tones {'ok' if res['topics']['tone_pass'] else 'FAIL'}")
    print(f"report: {a.report}")
    sys.exit(0 if planted_ok else 1)


if __name__ == "__main__":
    main()
