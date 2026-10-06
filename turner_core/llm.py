"""
llm.py — the model layer. Turner talks to one interface; behind it can be:

  * MLXBackend     any MLX model folder (or Hugging Face repo id), loaded in-process with mlx-lm
  * OpenAIBackend  any OpenAI-compatible server: LM Studio, `mlx_lm.server`, Ollama, vLLM, a hosted API

Models are named in models.json, discovered in ~/models, or given directly:

    gemma4-e2b                              an alias from models.json
    ~/models/mlx/Qwen3.5-4B-4bit            a folder
    mlx-community/SomeModel-4bit            a Hugging Face repo id (downloaded on first use)
    lmstudio                                whatever chat model LM Studio has loaded
    lmstudio:qwen3.8-27b                    a specific LM Studio model
    openai:MODEL@http://host:port/v1        any OpenAI-compatible endpoint

Every backend returns the same Reply, so the agent never sees model-specific formats.
"""

import gc
import glob
import json
import os
import re
import time

from . import toolcall

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_FILE = os.path.join(HERE, "models.json")
DISCOVER_DIRS = ["~/models/mlx/*", "~/models/original/*/*"]
LMSTUDIO_URL = "http://127.0.0.1:1234/v1"


class Reply:
    def __init__(self, thinking="", calls=None, text="", errors=None, raw="", usage=None):
        self.thinking, self.calls, self.text = thinking, calls or [], text
        self.errors, self.raw, self.usage = errors or [], raw, usage or {}


class Backend:
    kind = "?"
    name = "?"
    context = 14000          # prompt-token budget before the agent trims old tool output
    router_tokens = 8
    local = True             # False when prompts leave this machine
    tier = "small"           # "small": harness plans for the model · "large": model gets a plan tool and longer leash

    def chat(self, messages, tools=None, max_tokens=2048, temperature=0.7, thinking=True):
        raise NotImplementedError

    def count(self, messages, tools=None):
        return sum(len(str(m.get("content", ""))) + len(json.dumps(m.get("tool_calls", ""), default=str)) for m in messages) // 4 \
            + len(json.dumps(tools or "")) // 4

    def close(self):
        pass

    def describe(self):
        return "%s (%s)" % (self.name, self.kind)


# ── MLX, in-process ──────────────────────────────────────────────────────
class MLXBackend(Backend):
    kind = "mlx"

    def __init__(self, path, name=None, context=None, thinking=None, tier=None, **_):
        from mlx_lm import load                    # lazy: tests and server-only use don't need mlx
        self.path = os.path.expanduser(path)
        self.name = name or os.path.basename(self.path.rstrip("/"))
        t = time.time()
        self.model, self.tok = load(self.path)
        self.load_seconds = time.time() - t
        size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(self.path, "*.safetensors"))) if os.path.isdir(self.path) else 0
        self.tier = tier or ("large" if size > 9e9 else "small")         # weights over ~9 GB: treat as a capable model
        self.context = int(context or (24000 if self.tier == "large" else 14000))
        self.allow_thinking = True if thinking is None else bool(thinking)
        self.gemma = getattr(self.tok, "tool_call_start", None) == "<|tool_call>"
        self.native_tools = bool(getattr(self.tok, "has_tool_calling", False))
        self.last = {}

    def _prepare(self, messages, tools):
        if tools and not self.native_tools:
            return toolcall.flatten_for_prompt_tools(messages, tools), None
        return messages, tools or None

    def render(self, messages, tools=None, thinking=True):
        msgs, tl = self._prepare(messages, tools)
        try:
            return self.tok.apply_chat_template(msgs, tools=tl, add_generation_prompt=True, tokenize=False,
                                                enable_thinking=bool(thinking and self.allow_thinking))
        except Exception:
            # templates without a system role or tool support: fold everything into plain turns
            msgs = toolcall.flatten_for_prompt_tools(messages, tools)
            if msgs and msgs[0]["role"] == "system":
                sysmsg = msgs.pop(0)
                first_user = next((m for m in msgs if m["role"] == "user"), None)
                if first_user:
                    first_user["content"] = sysmsg["content"] + "\n\n" + first_user["content"]
            self.native_tools = False
            return self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)

    def count(self, messages, tools=None):
        return len(self.tok.encode(self.render(messages, tools), add_special_tokens=False))

    def chat(self, messages, tools=None, max_tokens=2048, temperature=0.7, thinking=True):
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler
        ids = self.tok.encode(self.render(messages, tools, thinking), add_special_tokens=False)
        sampler = make_sampler(temp=temperature, top_p=0.95, top_k=64) if temperature > 0 else make_sampler(temp=0.0)
        out, resp, t0 = [], None, time.time()
        limit = float(os.environ.get("TURNER_GEN_TIMEOUT", "300"))
        for resp in stream_generate(self.model, self.tok, prompt=ids, max_tokens=max_tokens, sampler=sampler):
            out.append(resp.text)
            if time.time() - t0 > limit:            # a stalled or runaway generation must not hang the agent
                break
        raw = "".join(out)
        usage = {"prompt_tokens": getattr(resp, "prompt_tokens", len(ids)),
                 "completion_tokens": getattr(resp, "generation_tokens", 0), "tps": getattr(resp, "generation_tps", 0.0)}
        if self.gemma:
            th, calls, text, errors = toolcall.parse(raw)
        else:
            th, calls, text, errors = toolcall.parse_generic(raw, self.tok, tools)
        return Reply(th, calls, text, errors, raw, usage)

    def close(self):
        self.model = self.tok = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass


# ── any OpenAI-compatible server ─────────────────────────────────────────
class OpenAIBackend(Backend):
    kind = "server"
    router_tokens = 400      # servers can't always switch reasoning off; leave room for it

    def __init__(self, base_url, model="auto", name=None, api_key_env=None, context=None, timeout=600, tier=None, **_):
        self.tier = tier or "large"
        import requests
        self.http = requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        key = os.environ.get(api_key_env or "", "") or "not-needed"
        self.http.headers.update({"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0].split(":")[0]
        self.local = host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")
        self.model = self._pick_model() if model in (None, "", "auto") else model
        self.name = name or self.model
        self.context = int(context or 24000)
        self.native_tools = True
        self.load_seconds = 0.0

    def _pick_model(self):
        try:
            r = self.http.get(self.base_url + "/models", timeout=5)
            r.raise_for_status()
            ids = [m["id"] for m in r.json().get("data", [])]
        except Exception as e:
            raise SystemExit("Can't reach the model server at %s (%s). Is it running?" % (self.base_url, type(e).__name__))
        chat = [i for i in ids if not re.search(r"embed|rerank|whisper|tts", i, re.I)]
        if not chat:
            raise SystemExit("The server at %s has no chat model available (found: %s). Load one first."
                             % (self.base_url, ", ".join(ids) or "nothing"))
        return chat[0]

    def available(self):
        try:
            r = self.http.get(self.base_url + "/models", timeout=5)
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return []

    @staticmethod
    def _wire(messages):
        out = []
        for m in messages:
            m = dict(m)
            if m.get("tool_calls"):
                m["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["function"]["name"],
                    "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False)}} for c in m["tool_calls"]]
                m["content"] = m.get("content") or ""
            if m["role"] == "tool":
                m = {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
            out.append(m)
        return out

    def chat(self, messages, tools=None, max_tokens=2048, temperature=0.7, thinking=True):
        if tools and not self.native_tools:
            payload_msgs, payload_tools = toolcall.flatten_for_prompt_tools(messages, tools), None
        else:
            payload_msgs, payload_tools = self._wire(messages), tools or None
        body = {"model": self.model, "messages": payload_msgs, "max_tokens": max_tokens, "temperature": temperature}
        if payload_tools:
            body["tools"] = payload_tools
        r = self.http.post(self.base_url + "/chat/completions", data=json.dumps(body), timeout=self.timeout)
        if r.status_code >= 400 and payload_tools:
            self.native_tools = False               # server or model rejects tools: describe them in the prompt instead
            return self.chat(messages, tools, max_tokens, temperature, thinking)
        if r.status_code >= 400:
            raise RuntimeError("model server error %d: %s" % (r.status_code, r.text[:300]))
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        thinking_text = msg.get("reasoning_content") or msg.get("reasoning") or ""
        m = re.search(r"<think>(.*?)</think>", content, re.S)
        if m:
            thinking_text, content = (thinking_text + "\n" + m.group(1)).strip(), content.replace(m.group(0), "")
        elif "</think>" in content:
            head, _, content = content.partition("</think>")
            thinking_text = (thinking_text + "\n" + head).strip()
        calls, errors = [], []
        for c in msg.get("tool_calls") or []:
            f = c.get("function", {})
            try:
                args = f.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
                calls.append({"name": f.get("name", ""), "arguments": args if isinstance(args, dict) else {}})
            except ValueError as e:
                errors.append("could not parse arguments of %s: %s" % (f.get("name"), e))
        text = content.strip()
        if not calls and not errors:                # some servers leave the call in the text
            _, calls, text, errors = toolcall.parse(content)
        u = data.get("usage") or {}
        return Reply(thinking_text, calls, text, errors, content,
                     {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)})

    def describe(self):
        return "%s (server %s%s)" % (self.name, self.base_url, "" if self.local else ", NOT local")


# ── registry ─────────────────────────────────────────────────────────────
def _is_mlx_dir(p):
    return os.path.isfile(os.path.join(p, "config.json")) and bool(glob.glob(os.path.join(p, "*.safetensors")))


def registry():
    """Named models: models.json entries plus MLX folders found under ~/models."""
    reg, default = {}, None
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE) as f:
                cfg = json.load(f)
            reg.update(cfg.get("models", {}))
            default = cfg.get("default")
        except ValueError as e:
            print("warning: models.json is not valid JSON (%s); ignoring it" % e)
    known = {os.path.realpath(os.path.expanduser(v.get("path", ""))) for v in reg.values() if v.get("backend", "mlx") == "mlx"}
    for pattern in DISCOVER_DIRS:
        for p in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.path.isdir(p) and _is_mlx_dir(p) and os.path.realpath(p) not in known:
                alias = os.path.basename(p).lower()
                if alias not in reg:
                    reg[alias] = {"backend": "mlx", "path": p, "discovered": True}
    return reg, default


def spec_to_config(spec):
    """Turn whatever the user typed into a backend config dict."""
    reg, default = registry()
    spec = (spec or os.environ.get("TURNER_MODEL") or default or "").strip()
    if not spec:
        mlx = [k for k, v in reg.items() if v.get("backend", "mlx") == "mlx"]
        if not mlx:
            raise SystemExit("No model configured. Add one to models.json or pass --model.")
        spec = mlx[0]
    if spec in reg:
        return dict(reg[spec], name=spec)
    low = spec.lower()
    if low in reg:
        return dict(reg[low], name=low)
    if low == "lmstudio" or low.startswith("lmstudio:"):
        return {"backend": "openai", "base_url": LMSTUDIO_URL, "model": spec.partition(":")[2] or "auto"}
    if low.startswith("openai:"):
        model, _, url = spec[7:].partition("@")
        if not url:
            raise SystemExit("Use openai:MODEL@http://host:port/v1")
        return {"backend": "openai", "base_url": url, "model": model or "auto"}
    if low.startswith("http://") or low.startswith("https://"):
        return {"backend": "openai", "base_url": spec, "model": "auto"}
    path = os.path.expanduser(spec)
    if os.path.isdir(path):
        if not _is_mlx_dir(path):
            raise SystemExit("%s is not an MLX model folder (needs config.json and .safetensors files). "
                             "GGUF models must be served by LM Studio instead: --model lmstudio" % path)
        return {"backend": "mlx", "path": path}
    if re.fullmatch(r"[\w.\-]+/[\w.\-]+", spec):
        return {"backend": "mlx", "path": spec, "name": spec.split("/")[-1], "remote_repo": True}
    close = [k for k in reg if low in k]
    hint = (" Did you mean: %s?" % ", ".join(close)) if close else " Run with --list-models to see what is available."
    raise SystemExit("Unknown model %r.%s" % (spec, hint))


def load_backend(spec=None):
    cfg = spec_to_config(spec)
    if cfg.get("backend", "mlx") == "openai":
        return OpenAIBackend(**{k: v for k, v in cfg.items() if k != "backend"})
    return MLXBackend(**{k: v for k, v in cfg.items() if k != "backend"})


def list_models():
    reg, default = registry()
    rows = []
    for name, cfg in reg.items():
        if cfg.get("backend", "mlx") == "openai":
            where = "server %s · model %s" % (cfg.get("base_url"), cfg.get("model", "auto"))
        else:
            where = os.path.expanduser(cfg.get("path", ""))
            where = where.replace(os.path.expanduser("~"), "~")
        rows.append((name, where, name == default, bool(cfg.get("discovered"))))
    return rows
