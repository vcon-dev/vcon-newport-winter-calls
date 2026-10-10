#!/usr/bin/env python3
"""Generate call transcripts from build/specs.jsonl via the Claude API.

Writes build/transcripts/<uuid>.json = {"uuid", "turns"}. Resumable: existing
transcripts are skipped unless --force. --only/--only-file imply --force for
those uuids (regeneration)."""
import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_common as lc  # noqa: E402

SPECS = lc.BUILD / "specs.jsonl"
OUT = lc.BUILD / "transcripts"
REJECTS = lc.BUILD / "generate-rejects.jsonl"
ERRORS = lc.BUILD / "generate-errors.jsonl"
PROMPT = "config/prompts/generate.md"
NOISE = ["[inaudible]", "[crosstalk]", "[line breaks up]", "[pause]"]
WORDS_PER_SEC = 2.3

SCHEMA = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["caller", "clerk"]},
                    "text": {"type": "string"},
                },
                "required": ["speaker", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["turns"],
    "additionalProperties": False,
}


def load_specs():
    if not SPECS.exists():
        sys.exit(f"error: {SPECS} not found; run scripts/plan_corpus.py first")
    return [json.loads(l) for l in SPECS.read_text().splitlines() if l.strip()]


def target_words(spec):
    return round(spec["duration_s"] * WORDS_PER_SEC)


def user_message(spec):
    body = {k: v for k, v in spec.items() if k not in ("index", "fixed_transcript")}
    body["target_words"] = target_words(spec)
    body["allowed_noise_markers"] = NOISE
    return json.dumps(body, indent=2, ensure_ascii=False)


def max_tokens_for(spec):
    return max(6000, target_words(spec) * 3 + 3000)  # headroom for any thinking tokens


def categories(spec):
    p = spec.get("planted") or {}
    cats = [k for k, v in p.items() if v]
    cats += ["decoy:" + d for d in spec.get("decoys") or []]
    return cats


def pick_sample(specs, n, seed):
    rng = random.Random(seed)
    pool = specs[:]
    rng.shuffle(pool)
    chosen, seen = [], set()
    # one per planted flag / decoy kind first, then random fill
    by_cat = {}
    for s in pool:
        for c in categories(s):
            by_cat.setdefault(c, []).append(s)
    for c in sorted(by_cat, key=lambda c: (c.startswith("decoy:"), c)):
        if len(chosen) >= n:
            break
        for s in by_cat[c]:
            if s["uuid"] not in seen:
                chosen.append(s)
                seen.add(s["uuid"])
                break
    for s in pool:
        if len(chosen) >= n:
            break
        if s["uuid"] not in seen:
            chosen.append(s)
            seen.add(s["uuid"])
    return chosen


# ---- post-checks -----------------------------------------------------------
def banned_terms(spec):
    """must_not entries are free text. Quoted strings inside an entry are used
    as terms; an unquoted entry of at most 2 words is used whole; longer prose
    entries (moves, not words) are left to the writer and the later lint."""
    terms = []
    for e in spec.get("must_not") or []:
        q = re.findall(r"[\"'“‘]([^\"'”’]+)[\"'”’]", e)
        if q:
            terms += q
        elif len(e.split()) <= 2 and re.fullmatch(r"[A-Za-z' -]+", e.strip()):
            terms.append(e.strip())
    return terms


def luhn(d):
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        s += n
        alt = not alt
    return s % 10 == 0


DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
DIGIT_WORD_RE = re.compile(r"\b(" + "|".join(DIGIT_WORDS) + r")\b", re.I)


def spoken_digits(text):
    return DIGIT_WORD_RE.sub(lambda m: DIGIT_WORDS[m.group(1).lower()], text)


def has_full_card(text):
    text = spoken_digits(text)
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text):
        d = re.sub(r"\D", "", m.group())
        if 13 <= len(d) <= 19 and luhn(d):
            return True
    return False


def misread_ok(num, phone, turns):
    """True when the stray number appears only in clerk turns and the caller gave the right one."""
    caller_said = any(phone[-4:] in re.sub(r"\D", "", spoken_digits(t["text"])) for t in turns if t["speaker"] == "caller")
    in_caller = any(num in re.sub(r"\D", "", spoken_digits(t["text"])) for t in turns if t["speaker"] == "caller")
    return caller_said and not in_caller


def check(spec, turns):
    fails = []
    if len(turns) < 4:
        fails.append(f"only {len(turns)} turns")
    if not turns or turns[0]["speaker"] != "clerk":
        fails.append("first speaker is not clerk")
    wc = sum(len(t["text"].split()) for t in turns)
    tw = target_words(spec)
    lo = 0.4 if tw > 800 else 0.5  # long calls include holds and silence
    if not (lo * tw <= wc <= 1.6 * tw):
        fails.append(f"word count {wc} outside {int(lo * 100)}%-160% of target {tw}")
    alltext = "\n".join(t["text"] for t in turns)
    for term in banned_terms(spec):
        if re.search(r"\b" + re.escape(term) + r"\b", alltext, re.I):
            fails.append(f"must_not word present: {term}")
    p = spec.get("planted") or {}
    caller = [t["text"] for t in turns if t["speaker"] == "caller"]
    if p.get("lawyer"):
        if not any(re.search(r"\blawyer", c, re.I) for c in caller):
            fails.append("planted lawyer: no caller turn says lawyer")
        if any(t["speaker"] == "clerk" and re.search(r"\blawyer", t["text"], re.I) for t in turns):
            fails.append("clerk says lawyer")
    elif re.search(r"\blawyers?\b", alltext, re.I):
        fails.append("word lawyer present but not planted")
    if re.search(r"\bayuh\b", alltext, re.I):
        fails.append("uses Ayuh")
    phone = (spec.get("caller") or {}).get("phone")
    for m in re.finditer(r"555\s*-?\s*01\d\d", re.sub(r"[^0-9\-\s]", " ", spoken_digits(alltext))):
        num = re.sub(r"\D", "", m.group())[-4:]
        # a clerk misreading the number back is realistic, as long as it is not a cluster household's number
        if not phone or (num != phone[-4:] and (int(num[-2:]) < 50 or not misread_ok(num, phone, turns))):
            fails.append(f"phone number 555-{num} is not the caller's assigned number")
    card = p.get("card_full")
    if card:
        digits = re.sub(r"\D", "", card["number"])
        if not any(digits in re.sub(r"\D", "", spoken_digits(c)) for c in caller):
            fails.append("planted card_full: card digits not in a single caller turn")
    elif has_full_card(alltext):
        fails.append("Luhn-valid card number present but not planted")
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated uuids (implies --force for them)")
    ap.add_argument("--only-file", help="file of uuids, one per line or a JSON list (implies --force)")
    ap.add_argument("--limit", type=int, help="process at most N pending specs")
    ap.add_argument("--sample", type=int, help="random pilot sample of N specs, stratified to cover planted flags and decoys")
    ap.add_argument("--seed", type=int, default=1, help="seed for --sample (default 1)")
    ap.add_argument("--force", action="store_true", help="regenerate even if a transcript exists")
    ap.add_argument("--dry-run", action="store_true", help="print one rendered request and exit; no generation")
    ap.add_argument("--count-tokens", action="store_true", help="with --dry-run: call count_tokens (free, needs credentials)")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-batch", action="store_true", help="synchronous requests, thread pool of 8 (small pilots)")
    ap.add_argument("--resume", action="store_true", help="collect a persisted unfinished batch instead of creating one")
    a = ap.parse_args()

    system = lc.load_prompt(PROMPT)
    if a.resume:
        specs_all = load_specs() if SPECS.exists() else []
        by_uuid = {s["uuid"]: s for s in specs_all}
        pending = []
    else:
        specs_all = load_specs()
        by_uuid = {s["uuid"]: s for s in specs_all}
        want = set()
        if a.only:
            want |= {x.strip() for x in a.only.split(",") if x.strip()}
        if a.only_file:
            txt = Path(a.only_file).read_text().strip()
            want |= set(json.loads(txt) if txt.startswith("[") else txt.split())
        if want:
            unknown = want - by_uuid.keys()
            if unknown:
                sys.exit(f"unknown uuids: {sorted(unknown)[:5]}")
        cand = [s for s in specs_all if not s.get("fixed_transcript")]
        if want:
            cand = [s for s in cand if s["uuid"] in want]
        elif not a.force:
            cand = [s for s in cand if not (OUT / f"{s['uuid']}.json").exists()]
        if a.sample:
            cand = pick_sample(cand, a.sample, a.seed)
        if a.limit:
            cand = cand[:a.limit]
        pending = cand

    if a.dry_run:
        if not pending:
            sys.exit("dry-run: no pending specs")
        s = pending[0]
        params = lc.make_params(a.model, system, user_message(s), SCHEMA, a.effort, max_tokens_for(s))
        print(json.dumps(params, indent=2, ensure_ascii=False))
        print(f"\n{len(pending)} pending specs; target_words for this one: {target_words(s)}")
        if a.count_tokens:
            import anthropic
            c = anthropic.Anthropic().messages.count_tokens(
                model=a.model, system=params["system"], messages=params["messages"])
            print(f"input tokens for this request: {c.input_tokens}")
        return

    import anthropic
    client = anthropic.Anthropic(max_retries=5)
    OUT.mkdir(parents=True, exist_ok=True)
    counts = {"written": 0, "rejected": 0, "errors": 0}

    def handle(cid, msg, err):
        spec = by_uuid.get(cid)
        if err or spec is None:
            counts["errors"] += 1
            lc.append_jsonl(ERRORS, {"ts": lc.now(), "uuid": cid, "error": err or "uuid not in specs"})
            return
        try:
            data = lc.message_json(msg)
            turns = [{"speaker": t["speaker"], "text": t["text"].strip()} for t in data["turns"]]
        except (ValueError, KeyError, TypeError) as ex:
            counts["errors"] += 1
            lc.append_jsonl(ERRORS, {"ts": lc.now(), "uuid": cid, "error": f"bad output: {ex}"})
            return
        fails = check(spec, turns)
        if fails:
            counts["rejected"] += 1
            lc.append_jsonl(REJECTS, {"ts": lc.now(), "uuid": cid, "reasons": fails, "turns": turns})
            return
        (OUT / f"{cid}.json").write_text(json.dumps({"uuid": cid, "turns": turns}, indent=2, ensure_ascii=False))
        counts["written"] += 1

    requests = [(s["uuid"], lc.make_params(a.model, system, user_message(s), SCHEMA, a.effort, max_tokens_for(s)))
                for s in pending]
    print(f"{len(requests)} specs to generate" if not a.resume else "resuming persisted batch")
    if a.no_batch:
        lc.run_sync(client, "generate", requests, handle)
    else:
        lc.run_batch(client, "generate", requests, handle, a.resume)
    print(f"written={counts['written']} rejected={counts['rejected']} errors={counts['errors']} "
          f"(rejects: {REJECTS.name}, errors: {ERRORS.name}); rerun to regenerate failures")


if __name__ == "__main__":
    main()
