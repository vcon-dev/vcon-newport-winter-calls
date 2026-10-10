"""Audit each drainage-cluster transcript against its household fact sheet.

The generator sometimes improvises small facts (depths, entry points, jobs,
spouses, who called when). This reads every cluster call with Claude, compares
it to the household sheet and storm dates in its spec, and writes the uuids of
calls with contradictions to build/cluster-audit-fail.txt for regeneration.
Eleanor Pucci's fixed transcript is authoritative and is not audited.

Usage: uv run python scripts/audit_cluster.py [--model claude-opus-5-5] [--effort high]
Cost estimates printed by llm_common use Sonnet rates; Opus 5.5 is 2x.
"""
import argparse
import json

import anthropic
import yaml

from llm_common import BUILD, ROOT, make_params, message_json, run_sync

SYSTEM = """You check one phone-call transcript for consistency against a fixed fact sheet.

You get: the household fact sheet for the caller's address (people, ages, jobs, spouses, phones, basement, flood entry points and depths, and a dated timeline of earlier calls and city actions), the call's date and weekday, the storm dates, and the transcript.

List every statement in the transcript, by either speaker, that contradicts the sheet or the dates: wrong depth, wrong entry point, wrong basement contents or finish, wrong job or retirement status, wrong spouse or relationship, wrong unit, wrong phone or phone kind, a flood the sheet does not have, a third basement flood, misstating who called or when, denying a city action that happened before this call, referring to an event dated after this call, or naming a storm day that does not match the storm dates. Ordinary conversational detail that the sheet is silent on is fine; only report real contradictions. Quote the exact words."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contradictions"],
    "properties": {
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["quote", "sheet_fact"],
                "properties": {"quote": {"type": "string"}, "sheet_fact": {"type": "string"}},
            },
        }
    },
}


def render(spec, turns, storms):
    who = {"caller": spec["caller"]["name"], "clerk": spec["clerk"]["name"]}
    lines = "\n".join(f"{who[t['speaker']]} ({t['speaker']}): {t['text']}" for t in turns)
    return (
        f"Call date: {spec['start']} ({spec.get('call_weekday')})\n"
        f"Caller: {spec['caller']['name']}\n\n"
        f"Storm dates:\n{json.dumps(storms, default=str)}\n\n"
        f"Household fact sheet:\n{json.dumps(spec['household'], indent=1)}\n\n"
        f"Transcript:\n{lines}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="high")
    args = ap.parse_args()

    storms = yaml.safe_load(open(ROOT / "config" / "corpus.yaml"))["storms"]
    specs = [json.loads(l) for l in open(BUILD / "specs.jsonl")]
    cluster = [s for s in specs if s["planted"]["cluster"] and not s.get("fixed_transcript")]
    requests = []
    for s in cluster:
        turns = json.load(open(BUILD / "transcripts" / f"{s['uuid']}.json"))["turns"]
        requests.append((s["uuid"], make_params(args.model, SYSTEM, render(s, turns, storms), SCHEMA, args.effort, 16000)))

    results = {}

    def handle(cid, msg, err):
        if err:
            results[cid] = {"error": err}
            return
        try:
            results[cid] = message_json(msg)
        except ValueError as e:
            results[cid] = {"error": str(e)}

    run_sync(anthropic.Anthropic(), "audit", requests, handle, workers=6)

    failed = sorted(u for u, r in results.items() if "error" in r or r["contradictions"])
    (BUILD / "cluster-audit.json").write_text(json.dumps(results, indent=1))
    (BUILD / "cluster-audit-fail.txt").write_text("\n".join(failed) + ("\n" if failed else ""))
    for u in failed:
        r = results[u]
        for c in r.get("contradictions", [])[:3]:
            print(f"{u[:8]}  \"{c['quote'][:90]}\"  vs  {c['sheet_fact'][:90]}")
        if "error" in r:
            print(f"{u[:8]}  error: {r['error']}")
    print(f"audited {len(results)} cluster calls; {len(failed)} with contradictions")


if __name__ == "__main__":
    main()
