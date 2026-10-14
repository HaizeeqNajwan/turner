#!/usr/bin/env python3
"""
turner_server.py — Turner as a small local web service, so a web interface can talk to it.

The model is loaded once and stays loaded. A web app (the Laravel project "chaturner") sends it
tasks over HTTP and follows their progress; Turner's own code is used unchanged.

    ~/models/venv312/bin/python turner_server.py --model gemma4-e2b

Design
  * One THREAD in the web app = one workspace folder + one Agent (its own conversation, summary,
    memory and file backups). Threads are isolated from each other.
  * ONE worker (the main thread, the one that loaded the model) runs tasks one at a time, because
    MLX must always be used from the thread that loaded it and a single GPU model cannot run two
    tasks at once. Extra tasks wait in a queue.
  * Approvals ("may I run this?") that the terminal asks with input() become events the web page
    answers: the task pauses until you click Allow, Always, or Decline.
  * Progress is a list of numbered events; clients poll "everything after event N" (long-poll).

Security
  * Listens on 127.0.0.1 only, and every request needs the shared token in the X-Turner-Token header.
    The token is read from TURNER_ENGINE_TOKEN or from web.token next to this file (created on first run).
  * Thread ids are validated; file access is confined to the thread's workspace and never reaches .turner.
"""

import argparse
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from turner_core import llm as llm_mod              # noqa: E402
from turner_core import skills                      # noqa: E402
from turner_core.agent import Agent                 # noqa: E402
from turner_core.safety import Denied, Policy       # noqa: E402
from turner_core.tools import Toolbox               # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
APPROVE_MODES = ("ask", "auto", "readonly")
MAX_BODY = 1_000_000
MAX_FILE_VIEW = 2_000_000
KEEP_RUNS = 200
APPROVAL_TIMEOUT = 600          # seconds a task waits for a click before it counts as declined


def brief(args, limit=240):
    """A short one-line description of tool arguments, for the activity feed."""
    parts = []
    for k, v in (args or {}).items():
        s = str(v).replace("\n", " ⏎ ")
        parts.append("%s=%s" % (k, s if len(s) <= 90 else s[:87] + "…"))
    out = ", ".join(parts)
    return out if len(out) <= limit else out[:limit] + "…"


# ── approvals over the web ───────────────────────────────────────────────
class WebPolicy(Policy):
    """The normal Policy, except 'may I?' questions go to the web page instead of the terminal."""

    run = None                    # the Run currently executing in this thread's workspace

    def confirm(self, kind, detail, reason):
        if self.approve == "auto" or kind in self.remembered:
            return
        if self.approve == "readonly":
            raise Denied("read-only mode: %s is disabled" % kind)
        run = self.run
        if run is None:
            raise Denied("%s needs the user's approval and no web session is attached." % kind)
        decision = run.ask_approval(kind, str(detail), reason)
        if decision == "always":
            self.remembered.add(kind)
            return
        if decision == "allow":
            return
        raise Denied("the user declined this %s. Do not retry it; explain what you wanted to do "
                     "or find another way." % kind)


# ── one task and its progress ────────────────────────────────────────────
class Run:
    def __init__(self, thread_id, text, skill=None, deep=False, approve="ask"):
        self.id = uuid.uuid4().hex[:16]
        self.thread_id, self.text = thread_id, text
        self.skill, self.deep, self.approve = skill, bool(deep), approve
        self.events, self.cond = [], threading.Condition()
        self.status = "queued"            # queued | running | waiting | done | error | cancelled
        self.answer, self.stats, self.error = None, {}, None
        self.cancelled = False
        self.approvals = {}               # id -> {"event": threading.Event, "decision": str | None}
        self.created, self.finished_at = time.time(), None

    # events
    def emit(self, type_, **data):
        with self.cond:
            self.events.append({"seq": len(self.events) + 1, "type": type_, "t": round(time.time() - self.created, 2), **data})
            self.cond.notify_all()

    def log(self, text):
        clean = ANSI.sub("", str(text)).rstrip()
        if not clean.strip():
            return
        s = clean.strip()
        kind = ("call" if s.startswith("→") else "mode" if s.startswith("[") else "nudge" if s.startswith("↺")
                else "warn" if s.startswith("!") else "info")
        self.emit("log", kind=kind, text=clean)

    @property
    def finished(self):
        return self.status in ("done", "error", "cancelled")

    def wait_events(self, after, timeout):
        """Return (events after `after`, finished?). Blocks up to `timeout` seconds for something new."""
        with self.cond:
            if len(self.events) <= after and not self.finished and timeout > 0:
                self.cond.wait(timeout)
            return list(self.events[after:]), self.finished

    def finish(self, status):
        self.status = status
        self.finished_at = time.time()
        self.emit("done", status=status)

    # approvals
    def ask_approval(self, kind, detail, reason):
        aid = uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "decision": None}
        self.approvals[aid] = slot
        previous, self.status = self.status, "waiting"
        self.emit("approval", id=aid, kind=kind, detail=detail[:1500], reason=reason)
        slot["event"].wait(APPROVAL_TIMEOUT)
        decision = slot["decision"] or "deny"          # no answer in time counts as no
        if not self.finished:
            self.status = previous if previous != "waiting" else "running"
        self.emit("approval_resolved", id=aid, decision=decision)
        return decision

    def resolve(self, aid, decision):
        slot = self.approvals.get(aid)
        if not slot or slot["event"].is_set():
            return False
        slot["decision"] = decision if decision in ("allow", "always", "deny") else "deny"
        slot["event"].set()
        return True

    def cancel(self):
        self.cancelled = True
        for slot in self.approvals.values():           # a pending question counts as "no"
            if not slot["event"].is_set():
                slot["decision"] = "deny"
                slot["event"].set()

    def snapshot(self):
        return {"id": self.id, "thread": self.thread_id, "status": self.status, "answer": self.answer, "stats": self.stats,
                "error": self.error, "events": len(self.events), "finished": self.finished}


class SwitchJob:
    """A request to change model. It rides the same queue as the runs, so it happens on the main thread
    (MLX must be used from the thread that loaded it) and only between tasks, never in the middle of one."""
    def __init__(self, key):
        self.key = key


# ── the engine ───────────────────────────────────────────────────────────
class Engine:
    def __init__(self, backend, threads_dir, token, model_key=None):
        self.llm, self.threads_dir, self.token = backend, os.path.realpath(threads_dir), token
        self.model_key = model_key or backend.name
        self.model_file = os.path.join(HERE, "web.model")     # remembers the last model picked in the interface
        self.switching = None             # name of the model being loaded right now (or waiting its turn)
        self.switch_error = None          # why the last switch failed, if it did
        os.makedirs(self.threads_dir, exist_ok=True)
        self.jobs = queue.Queue()
        self.runs, self.agents = {}, {}
        self.lock = threading.Lock()
        self.active = None                # the Run being executed right now

    # threads
    def workdir(self, tid):
        return os.path.join(self.threads_dir, tid)

    def agent_for(self, tid):
        with self.lock:
            agent = self.agents.get(tid)
            if agent:
                return agent
            policy = WebPolicy(self.workdir(tid), approve="ask", interactive=True)
            agent = Agent(self.llm, policy, max_steps=12, verbose=True)
            agent.web_run = None
            original = agent.toolbox.call

            def call(name, args, _orig=original, _agent=agent):
                run = _agent.web_run
                if run is not None:
                    if run.cancelled:
                        return False, "ERROR: the user cancelled this task. Stop now and tell the user briefly what was and was not done."
                    run.emit("tool", phase="start", name=name, args=brief(args))
                t0 = time.time()
                ok, out = _orig(name, args)
                if run is not None:
                    run.emit("tool", phase="end", name=name, ok=ok, ms=int((time.time() - t0) * 1000),
                             preview=ANSI.sub("", out)[:400], size=len(out))
                return ok, out

            agent.toolbox.call = call
            agent.resume()                # earlier conversation of this thread, if the engine was restarted
            self.agents[tid] = agent
            return agent

    def snapshot_files(self, workdir):
        out = {}
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
            for f in files:
                if f.startswith("."):
                    continue
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[os.path.relpath(p, workdir)] = (st.st_size, int(st.st_mtime_ns))
                if len(out) >= 500:
                    return out
        return out

    # runs
    def submit(self, tid, text, skill=None, deep=False, approve="ask"):
        if skill and skill not in skills.SKILLS:
            raise ValueError("unknown skill %r" % skill)
        if approve not in APPROVE_MODES:
            raise ValueError("approve must be one of: " + ", ".join(APPROVE_MODES))
        run = Run(tid, text, skill, deep, approve)
        self.runs[run.id] = run
        self._prune()
        self.jobs.put(run)
        run.emit("queued", ahead=self.jobs.qsize() - 1 + (1 if self.active else 0))
        return run

    def _prune(self):
        done = [r for r in self.runs.values() if r.finished]
        for r in sorted(done, key=lambda r: r.finished_at or 0)[:max(0, len(self.runs) - KEEP_RUNS)]:
            self.runs.pop(r.id, None)

    def execute(self, run):
        if run.cancelled:
            run.finish("cancelled")
            return
        agent = self.agent_for(run.thread_id)
        policy = agent.policy
        policy.approve, policy.run = run.approve, run
        agent.web_run, agent.log = run, run.log
        before = self.snapshot_files(policy.workdir)
        self.active = run
        run.status = "running"
        run.emit("start", model=self.llm.name, skill=run.skill or "auto", deep=run.deep, approve=run.approve)
        try:
            answer = agent.run(run.text, skill=run.skill, deep=run.deep)
            run.answer = answer
            run.stats = dict(agent.stats or {})
            after = self.snapshot_files(policy.workdir)
            changed = [{"path": p, "status": "new" if p not in before else "changed", "size": after[p][0]}
                       for p in sorted(after) if before.get(p) != after[p]]
            run.emit("files", files=changed)
            run.emit("answer", text=answer, stats=run.stats)
            run.finish("cancelled" if run.cancelled else "done")
        except Exception as e:                                # noqa: BLE001 — a failed task must not stop the engine
            run.error = "%s: %s" % (type(e).__name__, e)
            run.emit("error", message=run.error)
            run.finish("error")
        finally:
            self.active = None
            policy.run = None
            agent.web_run = None

    # model switching
    def request_switch(self, key):
        names = [n for n, _w, _d, _x in llm_mod.list_models()]
        if key not in names:
            raise ValueError("unknown model %r" % key)
        if self.switching:
            raise RuntimeError("already switching to %s" % self.switching)
        if key == self.model_key:
            return False
        self.switching, self.switch_error = key, None
        self.jobs.put(SwitchJob(key))
        return True

    def do_switch(self, job):
        try:
            new = llm_mod.load_backend(job.key)       # on the main thread, between tasks
        except Exception as e:                        # noqa: BLE001 — keep the old model if the new one will not load
            self.switch_error = "%s: %s" % (type(e).__name__, e)
            self.switching = None
            return
        with self.lock:
            old, self.llm, self.model_key = self.llm, new, job.key
            self.agents.clear()                       # agents were built on the old model; they resume from disk
        try:
            old.close()
        except Exception:                             # noqa: BLE001
            pass
        try:
            with open(self.model_file, "w") as f:
                f.write(job.key + "\n")
        except OSError:
            pass
        self.switching = None

    def work_loop(self):
        while True:
            run = self.jobs.get()
            if run is None:
                return
            if isinstance(run, SwitchJob):
                self.do_switch(run)
            else:
                self.execute(run)

    # files
    def safe_path(self, tid, rel):
        root = os.path.realpath(self.workdir(tid))
        p = os.path.realpath(os.path.join(root, rel or ""))
        if p != root and not p.startswith(root + os.sep):
            raise PermissionError("outside the workspace")
        parts = os.path.relpath(p, root).split(os.sep)
        if ".turner" in parts:
            raise PermissionError("that folder belongs to Turner")
        return p

    def list_files(self, tid):
        root = self.workdir(tid)
        if not os.path.isdir(root):
            return []
        snap = self.snapshot_files(root)
        return [{"path": p, "size": s, "modified": int(m / 1e9)} for p, (s, m) in sorted(snap.items())]

    def delete_thread(self, tid, files):
        with self.lock:
            self.agents.pop(tid, None)
        if files:
            root = os.path.realpath(self.workdir(tid))
            if os.path.dirname(root) == self.threads_dir and os.path.isdir(root):      # never anything outside threads/
                shutil.rmtree(root)
                return True
        return False


# ── HTTP ─────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    engine = None
    protocol_version = "HTTP/1.1"
    server_version = "TurnerEngine"

    def log_message(self, fmt, *args):
        pass

    # helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ValueError("body is not valid JSON")
        return data if isinstance(data, dict) else {}

    def authorised(self):
        given = self.headers.get("X-Turner-Token", "")
        return bool(given) and hmac.compare_digest(given, self.engine.token)

    def handle_any(self, method):
        try:
            if not self.authorised():
                return self.send_json({"error": "unauthorised"}, 401)
            url = urlparse(self.path)
            path, q = url.path.rstrip("/") or "/", {k: v[0] for k, v in parse_qs(url.query).items()}
            for verb, rx, fn in ROUTES:
                if verb == method:
                    m = rx.match(path)
                    if m:
                        return fn(self, q, *m.groups())
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except PermissionError as e:
            self.send_json({"error": str(e)}, 403)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                                            # noqa: BLE001
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


def need_thread(tid):
    if not THREAD_ID.match(tid):
        raise ValueError("bad thread id")
    return tid


def need_run(h, rid):
    if not RUN_ID.match(rid):
        raise ValueError("bad run id")
    run = h.engine.runs.get(rid)
    if not run:
        h.send_json({"error": "no such run (the engine may have restarted)"}, 404)
        return None
    return run


def r_health(h, q):
    e = h.engine
    h.send_json({"ok": True, "model": e.llm.name, "describe": e.llm.describe(), "busy": e.active is not None,
                 "queued": e.jobs.qsize(), "model_key": e.model_key, "switching": e.switching,
                 "switch_error": e.switch_error, "local": bool(getattr(e.llm, "local", True)),
                 "skills": {k: {"label": v["label"], "description": v["description"]} for k, v in skills.SKILLS.items()}})


def r_models(h, q):
    e = h.engine
    h.send_json({"current": e.model_key, "switching": e.switching, "switch_error": e.switch_error,
                 "models": [{"name": n, "where": w, "default": d, "current": n == e.model_key}
                            for n, w, d, _ in llm_mod.list_models()]})


def r_switch(h, q):
    key = str(h.read_json().get("model", "")).strip()
    try:
        started = h.engine.request_switch(key)
    except RuntimeError as err:
        return h.send_json({"error": str(err)}, 409)
    h.send_json({"switching": key if started else None, "current": h.engine.model_key}, 202 if started else 200)


def r_submit(h, q, tid):
    need_thread(tid)
    body = h.read_json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise ValueError("text is required")
    if len(text) > 20000:
        raise ValueError("text is too long (limit 20,000 characters)")
    run = h.engine.submit(tid, text, body.get("skill") or None, bool(body.get("deep")), body.get("approve") or "ask")
    h.send_json({"run": run.id, "status": run.status}, 201)


def r_run(h, q, rid):
    run = need_run(h, rid)
    if run:
        h.send_json(run.snapshot())


def r_events(h, q, rid):
    run = need_run(h, rid)
    if not run:
        return
    after = max(0, int(q.get("after", 0) or 0))
    wait = min(30.0, max(0.0, float(q.get("wait", 0) or 0)))
    events, finished = run.wait_events(after, wait)
    h.send_json({"events": events, "status": run.status, "finished": finished, "next": after + len(events)})


def r_approve(h, q, rid):
    run = need_run(h, rid)
    if not run:
        return
    body = h.read_json()
    ok = run.resolve(str(body.get("id", "")), str(body.get("decision", "")))
    h.send_json({"ok": ok}, 200 if ok else 409)


def r_cancel(h, q, rid):
    run = need_run(h, rid)
    if run:
        run.cancel()
        h.send_json({"ok": True})


def r_files(h, q, tid):
    need_thread(tid)
    h.send_json({"files": h.engine.list_files(tid)})


def r_file(h, q, tid):
    need_thread(tid)
    rel = q.get("path", "")
    if not rel:
        raise ValueError("path is required")
    p = h.engine.safe_path(tid, rel)
    if not os.path.isfile(p):
        return h.send_json({"error": "no such file"}, 404)
    size = os.path.getsize(p)
    if q.get("raw") == "1":                              # download
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(size))
        h.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(p).replace('"', ""))
        h.send_header("X-Content-Type-Options", "nosniff")
        h.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, h.wfile)
        return
    with open(p, "rb") as f:
        raw = f.read(MAX_FILE_VIEW)
    if b"\x00" in raw[:4000]:
        return h.send_json({"path": rel, "size": size, "binary": True})
    h.send_json({"path": rel, "size": size, "text": raw.decode("utf-8", errors="replace"), "truncated": size > MAX_FILE_VIEW})


def r_undo(h, q, tid):
    need_thread(tid)
    e = h.engine
    if e.active and e.active.thread_id == tid:
        raise ValueError("a task is running in this thread; wait for it to finish")
    agent = e.agent_for(tid)
    h.send_json({"undone": agent.policy.undo()})


def r_delete_thread(h, q, tid):
    need_thread(tid)
    e = h.engine
    if e.active and e.active.thread_id == tid:
        raise ValueError("a task is running in this thread; cancel it first")
    h.send_json({"deleted_files": e.delete_thread(tid, q.get("files") == "1")})


ROUTES = [
    ("GET", re.compile(r"^/health$"), r_health),
    ("GET", re.compile(r"^/models$"), r_models),
    ("POST", re.compile(r"^/model$"), r_switch),
    ("POST", re.compile(r"^/threads/([^/]+)/runs$"), r_submit),
    ("GET", re.compile(r"^/runs/([^/]+)$"), r_run),
    ("GET", re.compile(r"^/runs/([^/]+)/events$"), r_events),
    ("POST", re.compile(r"^/runs/([^/]+)/approve$"), r_approve),
    ("POST", re.compile(r"^/runs/([^/]+)/cancel$"), r_cancel),
    ("GET", re.compile(r"^/threads/([^/]+)/files$"), r_files),
    ("GET", re.compile(r"^/threads/([^/]+)/file$"), r_file),
    ("POST", re.compile(r"^/threads/([^/]+)/undo$"), r_undo),
    ("DELETE", re.compile(r"^/threads/([^/]+)$"), r_delete_thread),
]


def make_server(engine, host="127.0.0.1", port=8765):
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def load_token(path):
    env = os.environ.get("TURNER_ENGINE_TOKEN", "").strip()
    if env:
        return env
    if os.path.exists(path):
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def main():
    ap = argparse.ArgumentParser(description="Turner as a local web service")
    ap.add_argument("--model", help="model alias, MLX folder, HF repo id, lmstudio[:model], or openai:MODEL@URL")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1: this Mac only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--threads-dir", default=os.environ.get("TURNER_THREADS_DIR", os.path.join(HERE, "threads")),
                    help="where each thread's workspace folder lives")
    args = ap.parse_args()

    token_file = os.path.join(HERE, "web.token")
    token = load_token(token_file)
    spec = args.model
    if not spec:                              # the last model picked in the web interface
        try:
            with open(os.path.join(HERE, "web.model")) as f:
                saved = f.read().strip()
            if saved in [n for n, _w, _d, _x in llm_mod.list_models()]:
                spec = saved
        except OSError:
            pass
    print("loading model…", flush=True)
    backend = llm_mod.load_backend(spec)
    if not getattr(backend, "local", True):
        print("Note: %s is not on this machine; prompts and file contents will be sent to it." % backend.describe())
    tool_names = {getattr(Toolbox, a).tool_name for a in dir(Toolbox) if hasattr(getattr(Toolbox, a), "tool_name")}
    for name, problem in skills.load_custom(os.path.join(HERE, "skills"), tool_names):
        print("warning: skill file %s skipped: %s" % (name, problem))

    engine = Engine(backend, args.threads_dir, token, spec or llm_mod.registry()[1])
    httpd = make_server(engine, args.host, args.port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("Turner engine ready · %s · http://%s:%d · threads in %s" % (backend.describe(), args.host, args.port, engine.threads_dir), flush=True)
    print("token: %s (or set TURNER_ENGINE_TOKEN)" % ("from the environment" if os.environ.get("TURNER_ENGINE_TOKEN") else token_file), flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening beyond this Mac. Anyone with the token can make Turner run code here.", flush=True)
    try:
        engine.work_loop()                    # the main thread does the model work, always
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.shutdown()
        backend.close()


if __name__ == "__main__":
    main()
