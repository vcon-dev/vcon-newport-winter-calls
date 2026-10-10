"""Shared helpers for generate.py and verify.py: request building, Message
Batches flow (persist ids, poll, stream results), a synchronous fallback,
usage logging and cost estimates. Sonnet 5.5 rates: $2 in / $10 out per MTok,
cache write 1.25x input, cache read 0.1x input, Batches 50% off."""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
BATCH_STATE = BUILD / "batches.json"

IN_RATE, OUT_RATE = 2.0, 10.0  # USD per MTok, standard
BATCH_CHUNK = 10_000


def load_prompt(rel):
    p = ROOT / rel
    if not p.exists():
        sys.exit(f"error: system prompt missing: {p}. It is written separately; create it first.")
    return p.read_text()


def make_params(model, system_text, user_text, schema, effort, max_tokens):
    """Messages params shared by batch and sync paths. No thinking param (omit
    on Sonnet 5.5), no prefill, no forced tool_choice. JSON via structured
    outputs; cache_control on the system block."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }


def message_json(msg):
    """Return parsed JSON from a finished message, or raise ValueError."""
    if msg.stop_reason == "max_tokens":
        raise ValueError("truncated: stop_reason=max_tokens")
    if msg.stop_reason == "refusal":
        raise ValueError("model refusal")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise ValueError("no text block")
    return json.loads(text)


def usage_dict(msg):
    u = msg.usage
    return {
        "input": u.input_tokens or 0,
        "output": u.output_tokens or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tally:
    def __init__(self):
        self.i = self.o = self.cr = self.cw = 0

    def add(self, u):
        self.i += u["input"]
        self.o += u["output"]
        self.cr += u["cache_read"]
        self.cw += u["cache_write"]

    def cost(self, batch):
        f = 0.5 if batch else 1.0
        return f * (self.i * IN_RATE + self.cw * IN_RATE * 1.25 + self.cr * IN_RATE * 0.1 + self.o * OUT_RATE) / 1e6

    def line(self, batch):
        return (f"tokens in={self.i} out={self.o} cache_read={self.cr} cache_write={self.cw}; "
                f"est. cost ${self.cost(batch):.2f} ({'batch' if batch else 'standard'} rates)")


# ---- batch state -----------------------------------------------------------
def _state():
    if BATCH_STATE.exists():
        return json.loads(BATCH_STATE.read_text())
    return {}


def _save(state):
    BATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BATCH_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(BATCH_STATE)


def run_batch(client, kind, requests, handle, resume):
    """requests: list of (custom_id, params). handle(custom_id, message_or_None, error_str_or_None).
    Persists batch ids under state[kind] immediately after creation."""
    state = _state()
    entries = state.get(kind, [])
    open_entries = [e for e in entries if not e.get("done")]
    if resume:
        if not open_entries:
            sys.exit(f"--resume: no unfinished {kind} batch in {BATCH_STATE}")
    else:
        if open_entries:
            sys.exit(f"unfinished {kind} batch(es) in {BATCH_STATE}: {[e['id'] for e in open_entries]}. "
                     f"Use --resume to collect them, or mark them done/remove them to start over.")
        if not requests:
            print("nothing to do")
            return
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
        for k in range(0, len(requests), BATCH_CHUNK):
            chunk = requests[k:k + BATCH_CHUNK]
            b = client.messages.batches.create(requests=[
                Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**p)) for cid, p in chunk])
            entries.append({"id": b.id, "n": len(chunk), "created": now(), "done": False})
            state[kind] = entries
            _save(state)  # persist before anything else can fail
            print(f"created batch {b.id} ({len(chunk)} requests)")
        open_entries = [e for e in entries if not e.get("done")]

    tally = Tally()
    for e in open_entries:
        delay = 20
        try:
            while True:
                b = client.messages.batches.retrieve(e["id"])
                rc = b.request_counts
                print(f"{e['id']}: {b.processing_status} processing={rc.processing} "
                      f"ok={rc.succeeded} err={rc.errored} exp={rc.expired}", flush=True)
                if b.processing_status == "ended":
                    break
                time.sleep(delay)
                delay = min(int(delay * 1.5), 300)
        except KeyboardInterrupt:
            sys.exit(f"interrupted; batch id saved in {BATCH_STATE}; rerun with --resume")
        for r in client.messages.batches.results(e["id"]):
            t = r.result.type
            if t == "succeeded":
                msg = r.result.message
                u = usage_dict(msg)
                tally.add(u)
                append_jsonl(BUILD / "usage.jsonl",
                             {"ts": now(), "kind": kind, "custom_id": r.custom_id, "batch": True, **u})
                handle(r.custom_id, msg, None)
            else:
                err = t
                if t == "errored":
                    er = r.result.error
                    err = f"errored: {getattr(getattr(er, 'error', er), 'type', '?')}: {getattr(getattr(er, 'error', er), 'message', '')}"
                handle(r.custom_id, None, err)
        e["done"] = True
        e["finished"] = now()
        state = _state()
        state[kind] = [x if x["id"] != e["id"] else e for x in state.get(kind, [])]
        _save(state)
    print(tally.line(True))


def run_sync(client, kind, requests, handle, workers=8, attempts=5):
    import anthropic
    tally = Tally()

    def one(cid, params):
        for a in range(attempts):
            try:
                return client.messages.create(**params)
            except anthropic.APIStatusError as ex:
                if ex.status_code == 429 or ex.status_code >= 500:
                    time.sleep(min(2 ** a * 2, 60))
                    continue
                raise
            except anthropic.APIConnectionError:
                time.sleep(min(2 ** a * 2, 60))
        raise RuntimeError("retries exhausted")

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(one, cid, p): cid for cid, p in requests}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                msg = fut.result()
            except Exception as exc:  # noqa: BLE001
                handle(cid, None, f"{type(exc).__name__}: {exc}")
                continue
            u = usage_dict(msg)
            tally.add(u)
            append_jsonl(BUILD / "usage.jsonl", {"ts": now(), "kind": kind, "custom_id": cid, "batch": False, **u})
            handle(cid, msg, None)
    print(tally.line(False))
