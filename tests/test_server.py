"""Offline checks for turner_server.py (the web engine): runs, events, approvals, files, auth. No real model is loaded.

Run:  ~/models/venv312/bin/python tests/test_server.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import turner_server as srv                                    # noqa: E402
from turner_core import llm as llm_mod                         # noqa: E402

passed = failed = 0
TOKEN = "test-token-123"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


class FakeLLM(llm_mod.Backend):
    """Plays back a script: each chat() call returns the next scripted reply."""
    kind, name, context, router_tokens, local, tier = "fake", "fake-1", 14000, 8, True, "small"

    def __init__(self, script=()):
        self.script = list(script)

    def chat(self, messages, tools=None, max_tokens=2048, temperature=0.7, thinking=True):
        r = self.script.pop(0) if self.script else {"text": "(script finished)"}
        return llm_mod.Reply(calls=r.get("calls"), text=r.get("text", ""))


def call(name, args):
    return {"calls": [{"name": name, "arguments": args}]}


def start(script):
    tmp = tempfile.mkdtemp(prefix="turner_srv_")
    engine = srv.Engine(FakeLLM(script), os.path.join(tmp, "threads"), TOKEN)
    httpd = srv.make_server(engine, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=engine.work_loop, daemon=True).start()
    return engine, httpd, "http://127.0.0.1:%d" % httpd.server_address[1], tmp


def http(base, method, path, body=None, token=TOKEN):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-Turner-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def follow(base, run_id, until_approval=False, timeout=20):
    """Long-poll the run's events like the web app does. Returns all events, or stops at the first approval."""
    seen, after, t0 = [], 0, time.time()
    while time.time() - t0 < timeout:
        s, d = http(base, "GET", "/runs/%s/events?after=%d&wait=2" % (run_id, after))
        seen += d["events"]
        after = d["next"]
        if until_approval and any(e["type"] == "approval" for e in d["events"]):
            return seen
        if d["finished"]:
            return seen
    return seen


def types(events):
    return [e["type"] for e in events]


TID = "thread-aaaa1111"

# ── auth and basics ──────────────────────────────────────────────────────
engine, httpd, base, tmp = start([{"text": "Hello from the fake model."}])
check("no token -> 401", http(base, "GET", "/health", token=None)[0] == 401)
check("wrong token -> 401", http(base, "GET", "/health", token="nope")[0] == 401)
s, d = http(base, "GET", "/health")
check("health ok", s == 200 and d["ok"] and d["model"] == "fake-1")
check("health lists modes", "code" in d["skills"] and "risk" in d["skills"])
check("unknown route -> 404", http(base, "GET", "/nope")[0] == 404)

# ── a plain run ──────────────────────────────────────────────────────────
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "say hello", "skill": "chat"})
check("submit -> 201", s == 201 and d["run"])
ev = follow(base, d["run"])
check("events: queued, start, answer, done", types(ev)[0] == "queued" and "start" in types(ev) and "answer" in types(ev) and types(ev)[-1] == "done")
ans = next(e for e in ev if e["type"] == "answer")
check("answer text delivered", ans["text"] == "Hello from the fake model.")
check("stats delivered", ans["stats"]["steps"] >= 1)
check("events are numbered in order", [e["seq"] for e in ev] == list(range(1, len(ev) + 1)))
s, snap = http(base, "GET", "/runs/%s" % d["run"])
check("run snapshot says done", snap["status"] == "done" and snap["answer"] == "Hello from the fake model.")
check("workspace folder created for the thread", os.path.isdir(os.path.join(tmp, "threads", TID)))

# ── validation ───────────────────────────────────────────────────────────
check("empty text rejected", http(base, "POST", "/threads/%s/runs" % TID, {"text": "  "})[0] == 400)
check("bad thread id rejected", http(base, "POST", "/threads/..%2f..%2fetc/runs", {"text": "x"})[0] in (400, 404))
check("unknown skill rejected", http(base, "POST", "/threads/%s/runs" % TID, {"text": "x", "skill": "wizard"})[0] == 400)
check("bad approve mode rejected", http(base, "POST", "/threads/%s/runs" % TID, {"text": "x", "approve": "yolo"})[0] == 400)
check("too long rejected", http(base, "POST", "/threads/%s/runs" % TID, {"text": "x" * 20001})[0] == 400)
check("unknown run -> 404", http(base, "GET", "/runs/doesnotexist1/events")[0] == 404)
httpd.shutdown()

# ── a run that uses a tool, creates a file, and is reported as such ──────
script = [call("write_file", {"path": "notes.txt", "content": "alpha"}), {"text": "Saved notes.txt."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "save notes.txt with the word alpha", "skill": "write"})
ev = follow(base, d["run"])
tool_ev = [e for e in ev if e["type"] == "tool"]
check("tool start and end events", [e["phase"] for e in tool_ev] == ["start", "end"] and tool_ev[1]["ok"] is True)
files_ev = next(e for e in ev if e["type"] == "files")
check("new file reported", files_ev["files"] == [{"path": "notes.txt", "status": "new", "size": 5}])
s, d3 = http(base, "GET", "/threads/%s/files" % TID)
check("file listing", [f["path"] for f in d3["files"]] == ["notes.txt"])
s, d3 = http(base, "GET", "/threads/%s/file?path=notes.txt" % TID)
check("file content", s == 200 and d3["text"] == "alpha")
s, raw = http(base, "GET", "/threads/%s/file?path=notes.txt&raw=1" % TID)
check("raw download", s == 200 and raw == b"alpha")
check("path traversal refused", http(base, "GET", "/threads/%s/file?path=../../../../etc/hosts" % TID)[0] == 403)
check("absolute path refused", http(base, "GET", "/threads/%s/file?path=/etc/hosts" % TID)[0] == 403)
check(".turner folder refused", http(base, "GET", "/threads/%s/file?path=.turner/audit.jsonl" % TID)[0] == 403)
check("missing file -> 404", http(base, "GET", "/threads/%s/file?path=nope.txt" % TID)[0] == 404)
s, u = http(base, "POST", "/threads/%s/undo" % TID)
check("undo removes the file the task created", u["undone"] and not os.path.exists(os.path.join(tmp, "threads", TID, "notes.txt")))
httpd.shutdown()

# ── approvals: allow ─────────────────────────────────────────────────────
script = [call("run_shell", {"command": "touch made.txt"}), {"text": "Made it."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "create made.txt with touch", "skill": "code"})
ev = follow(base, d["run"], until_approval=True)
ap = next((e for e in ev if e["type"] == "approval"), None)
check("approval requested for a risky shell command", ap is not None and "touch made.txt" in ap["detail"] and ap["kind"] == "shell command")
s, snap = http(base, "GET", "/runs/%s" % d["run"])
check("run is 'waiting' while a question is open", snap["status"] == "waiting")
check("file not made before approval", not os.path.exists(os.path.join(tmp, "threads", TID, "made.txt")))
s, r = http(base, "POST", "/runs/%s/approve" % d["run"], {"id": ap["id"], "decision": "allow"})
check("approve accepted", s == 200 and r["ok"])
check("answering twice is refused", http(base, "POST", "/runs/%s/approve" % d["run"], {"id": ap["id"], "decision": "allow"})[0] == 409)
ev2 = follow(base, d["run"])
check("run finishes after approval", types(ev2)[-1] == "done")
check("approved command ran", os.path.exists(os.path.join(tmp, "threads", TID, "made.txt")))
httpd.shutdown()

# ── approvals: decline ───────────────────────────────────────────────────
script = [call("run_shell", {"command": "touch nope.txt"}), {"text": "Understood, I did not run it."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "create nope.txt with touch", "skill": "code"})
ev = follow(base, d["run"], until_approval=True)
ap = next(e for e in ev if e["type"] == "approval")
http(base, "POST", "/runs/%s/approve" % d["run"], {"id": ap["id"], "decision": "deny"})
ev2 = follow(base, d["run"])
end = [e for e in ev2 if e["type"] == "tool" and e["phase"] == "end"]
check("declined command did not run", not os.path.exists(os.path.join(tmp, "threads", TID, "nope.txt")))
check("model was told it was declined", end and "declined" in end[0]["preview"].lower() and end[0]["ok"] is False)
httpd.shutdown()

# ── approvals: 'always' remembers for the thread, 'auto' never asks ──────
script = [call("run_shell", {"command": "touch a1.txt"}), call("run_shell", {"command": "touch a2.txt"}), {"text": "Both done."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "make a1.txt and a2.txt", "skill": "code"})
ev = follow(base, d["run"], until_approval=True)
ap = next(e for e in ev if e["type"] == "approval")
http(base, "POST", "/runs/%s/approve" % d["run"], {"id": ap["id"], "decision": "always"})
ev2 = follow(base, d["run"])
check("'always' asks only once for the same kind of action", sum(1 for e in ev2 if e["type"] == "approval") == 1)
check("both commands ran", all(os.path.exists(os.path.join(tmp, "threads", TID, f)) for f in ("a1.txt", "a2.txt")))
httpd.shutdown()

script = [call("run_shell", {"command": "touch auto.txt"}), {"text": "Done."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "make auto.txt", "skill": "code", "approve": "auto"})
ev = follow(base, d["run"])
check("auto mode never asks", "approval" not in types(ev) and os.path.exists(os.path.join(tmp, "threads", TID, "auto.txt")))
httpd.shutdown()

script = [call("run_shell", {"command": "touch ro.txt"}), {"text": "I could not."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "make ro.txt", "skill": "code", "approve": "readonly"})
ev = follow(base, d["run"])
check("read-only mode refuses without asking", "approval" not in types(ev) and not os.path.exists(os.path.join(tmp, "threads", TID, "ro.txt")))
httpd.shutdown()

# ── cancelling ───────────────────────────────────────────────────────────
script = [call("run_shell", {"command": "touch c.txt"}), {"text": "Stopped."}]
engine, httpd, base, tmp = start(script)
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "make c.txt", "skill": "code"})
ev = follow(base, d["run"], until_approval=True)
http(base, "POST", "/runs/%s/cancel" % d["run"])
ev2 = follow(base, d["run"])
s, snap = http(base, "GET", "/runs/%s" % d["run"])
check("cancel declines the open question and ends the run", snap["status"] == "cancelled" and not os.path.exists(os.path.join(tmp, "threads", TID, "c.txt")))
httpd.shutdown()

# ── two runs queue; threads are isolated; a crash does not kill the engine ──
class Boom(FakeLLM):
    def chat(self, *a, **k):
        raise RuntimeError("model fell over")

engine, httpd, base, tmp = start([])
engine.llm = Boom()
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "anything", "skill": "chat"})
ev = follow(base, d["run"])
check("a model crash becomes an error event", "error" in types(ev) and types(ev)[-1] == "done")
s, snap = http(base, "GET", "/runs/%s" % d["run"])
check("run status is error", snap["status"] == "error" and "fell over" in snap["error"])
engine.llm = FakeLLM([{"text": "still alive"}])
engine.agents.clear()
s, d = http(base, "POST", "/threads/%s/runs" % TID, {"text": "hello again", "skill": "chat"})
ev = follow(base, d["run"])
check("engine keeps working after a crash", next(e for e in ev if e["type"] == "answer")["text"] == "still alive")

engine.llm = FakeLLM([{"text": "one"}, {"text": "two"}])
engine.agents.clear()
a = http(base, "POST", "/threads/thread-bbbb2222/runs", {"text": "first", "skill": "chat"})[1]["run"]
b = http(base, "POST", "/threads/thread-cccc3333/runs", {"text": "second", "skill": "chat"})[1]["run"]
ea, eb = follow(base, a), follow(base, b)
check("two threads both complete", "answer" in types(ea) and "answer" in types(eb))
check("each thread has its own folder", all(os.path.isdir(os.path.join(tmp, "threads", t)) for t in ("thread-bbbb2222", "thread-cccc3333")))

# ── deleting a thread ────────────────────────────────────────────────────
s, r = http(base, "DELETE", "/threads/thread-bbbb2222?files=1")
check("thread files removed on request", s == 200 and r["deleted_files"] and not os.path.exists(os.path.join(tmp, "threads", "thread-bbbb2222")))
s, r = http(base, "DELETE", "/threads/thread-cccc3333")
check("thread files kept by default", s == 200 and not r["deleted_files"] and os.path.isdir(os.path.join(tmp, "threads", "thread-cccc3333")))
# ── switching model ──────────────────────────────────────────────────────
real_list, real_load = llm_mod.list_models, llm_mod.load_backend
loaded = []


class FakeB(FakeLLM):
    name = "fake-2"
    closed = False

    def close(self):
        FakeB.closed = True


def fake_load(spec=None):
    loaded.append((spec, threading.current_thread() is threading.main_thread()))
    if spec == "broken":
        raise RuntimeError("no weights here")
    return FakeB([{"text": "from the new model"}])


llm_mod.list_models = lambda: [("fake-1", "~/x", True, False), ("second", "~/y", False, False), ("broken", "~/z", False, False)]
llm_mod.load_backend = fake_load
engine.model_key = "fake-1"
engine.model_file = os.path.join(tmp, "web.model")
old_llm = engine.llm
s, d = http(base, "GET", "/models")
check("models list marks the current one", s == 200 and [m["name"] for m in d["models"] if m["current"]] == ["fake-1"])
check("switching to an unknown model is refused", http(base, "POST", "/model", {"model": "nope"})[0] == 400)
check("switching needs the token", http(base, "POST", "/model", {"model": "second"}, token=None)[0] == 401)
check("switching to the current model does nothing", http(base, "POST", "/model", {"model": "fake-1"})[0] == 200 and not loaded)
s, d = http(base, "POST", "/model", {"model": "broken"})
t0 = time.time()
while engine.switching and time.time() - t0 < 5:
    time.sleep(0.05)
s, h = http(base, "GET", "/health")
check("a model that fails to load leaves the old one in place", engine.llm is old_llm and h["model_key"] == "fake-1" and "no weights" in (h["switch_error"] or ""))
s, d = http(base, "POST", "/model", {"model": "second"})
check("a switch is accepted and reported", s == 202 and d["switching"] == "second")
t0 = time.time()
while engine.switching and time.time() - t0 < 5:
    time.sleep(0.05)
s, h = http(base, "GET", "/health")
check("the switch completes", h["model_key"] == "second" and h["switching"] is None and h["switch_error"] is None and engine.llm.name == "fake-2")
check("the choice is remembered for the next start", open(engine.model_file).read().strip() == "second")
check("agents were rebuilt for the new model", engine.agents == {})
s, d = http(base, "POST", "/threads/thread-dddd4444/runs", {"text": "hi", "skill": "chat"})
ev = follow(base, d["run"])
check("a run after the switch uses the new model", next(e for e in ev if e["type"] == "start")["model"] == "fake-2"
      and next(e for e in ev if e["type"] == "answer")["text"] == "from the new model")
llm_mod.list_models, llm_mod.load_backend = real_list, real_load

httpd.shutdown()

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
