"""
toolcall.py — parse Gemma-4 output into (thinking, tool calls, visible text).

Gemma 4 emits, natively:

    <|channel>thought ... <channel|>                      (reasoning, optional)
    <|tool_call>call:NAME{key:<|"|>text<|"|>,n:3}<tool_call|>   (zero or more)
    plain text                                            (the answer)

Arguments are a JSON-like object whose keys are bare words and whose strings are
delimited by <|"|> instead of quotes. This module turns that into plain Python.
Small models occasionally fall back to ordinary JSON, so that is accepted too.
"""

import json
import re

Q = '<|"|>'
THINK_RE = re.compile(r"<\|channel>(?:thought)?\n?(.*?)(?:<channel\|>|$)", re.S)
CALL_RE = re.compile(r"<\|tool_call>\s*call:\s*([\w.\-]+)\s*(\{.*?)\s*(?:<tool_call\|>|$)", re.S)
JSON_CALL_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.S)
SPECIAL_RE = re.compile(r"<\|?(?:tool_call|tool_response|turn|channel|think|tool)\|?>")


class ParseError(ValueError):
    pass


class _Reader:
    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def value(self):
        self.ws()
        s, i = self.s, self.i
        if s.startswith(Q, i):
            end = s.find(Q, i + len(Q))
            if end < 0:                       # unterminated string: take the rest
                self.i = len(s)
                return s[i + len(Q):]
            self.i = end + len(Q)
            return s[i + len(Q):end]
        if s[i:i + 1] in ('"', "'"):
            quote, j, out = s[i], i + 1, []
            while j < len(s) and s[j] != quote:
                if s[j] == "\\" and j + 1 < len(s):
                    out.append({"n": "\n", "t": "\t", "r": "\r"}.get(s[j + 1], s[j + 1]))
                    j += 2
                else:
                    out.append(s[j])
                    j += 1
            self.i = j + 1
            return "".join(out)
        if s[i:i + 1] == "{":
            return self.obj()
        if s[i:i + 1] == "[":
            self.i += 1
            items = []
            while True:
                self.ws()
                if self.peek() == "]":
                    self.i += 1
                    return items
                if self.i >= len(s):
                    return items
                items.append(self.value())
                self.ws()
                if self.peek() == ",":
                    self.i += 1
        m = re.compile(r"[^,}\]\s]+").match(s, i)
        if not m:
            raise ParseError("expected a value at position %d" % i)
        self.i = m.end()
        tok = m.group(0)
        low = tok.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("null", "none"):
            return None
        try:
            return int(tok)
        except ValueError:
            try:
                return float(tok)
            except ValueError:
                return tok

    def obj(self):
        self.ws()
        if self.peek() != "{":
            raise ParseError("expected '{'")
        self.i += 1
        out = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.i += 1
                return out
            if self.i >= len(self.s):
                return out                      # tolerate a missing closing brace
            if self.s.startswith(Q, self.i) or self.peek() in ('"', "'"):
                key = self.value()
            else:
                m = re.compile(r"[\w.\-]+").match(self.s, self.i)
                if not m:
                    raise ParseError("bad argument name at position %d" % self.i)
                key = m.group(0)
                self.i = m.end()
            self.ws()
            if self.peek() not in (":", "="):
                raise ParseError("expected ':' after %r" % key)
            self.i += 1
            out[key] = self.value()
            self.ws()
            if self.peek() == ",":
                self.i += 1


def parse_args(text):
    return _Reader(text).obj()


def parse(raw):
    """Return (thinking, calls, text, errors).

    calls  -> list of {"name": str, "arguments": dict}
    errors -> list of human-readable strings for calls that could not be parsed
    """
    thinking = "\n".join(t.strip() for t in THINK_RE.findall(raw)).strip()
    body = THINK_RE.sub("", raw)
    calls, errors = [], []
    for m in CALL_RE.finditer(body):
        name, args = m.group(1), m.group(2)
        try:
            calls.append({"name": name, "arguments": parse_args(args)})
        except (ParseError, IndexError) as e:
            errors.append("could not parse arguments of %s: %s" % (name, e))
    text = CALL_RE.sub("", body)
    if not calls and not errors:
        # JSON fallback: {"name": ..., "arguments": {...}} inside a code fence
        for m in JSON_CALL_RE.finditer(text):
            try:
                d = json.loads(m.group(1))
            except ValueError:
                continue
            name = d.get("name") or d.get("tool")
            args = d.get("arguments", d.get("parameters", d.get("args")))
            if isinstance(name, str) and isinstance(args, dict):
                calls.append({"name": name, "arguments": args})
                text = text.replace(m.group(0), "")
    text = SPECIAL_RE.sub("", text).strip()
    return thinking, calls, text, errors


# ── any other model family ───────────────────────────────────────────────
def parse_generic(raw, tok, tools=None):
    """Parse output from a non-Gemma MLX model using the markers and parser mlx-lm found for it.

    `tok` is the mlx-lm TokenizerWrapper: it knows the model's thinking markers
    (think_start/think_end), its tool-call markers and how to decode a call.
    """
    thinking, body = "", raw
    ts, te = getattr(tok, "think_start", None), getattr(tok, "think_end", None)
    if te and te in body:
        head, _, body = body.partition(te)          # the opening marker is often part of the prompt
        thinking = head.replace(ts or "\x00", "").strip()
    elif ts and ts in body:                         # thinking never closed: all of it is reasoning
        before, _, rest = body.partition(ts)
        thinking, body = rest.strip(), before
    calls, errors = [], []
    start, end = getattr(tok, "tool_call_start", None), getattr(tok, "tool_call_end", None)
    parser = getattr(tok, "tool_parser", None)
    if start and parser and start in body:
        pieces = body.split(start)
        text = pieces[0]
        for piece in pieces[1:]:
            seg, _, after = piece.partition(end) if end else (piece, "", "")
            text += after
            try:
                got = parser(seg, tools)
                for c in (got if isinstance(got, list) else [got]):
                    args = c.get("arguments", {})
                    if isinstance(args, str):
                        args = json.loads(args) if args.strip() else {}
                    calls.append({"name": c["name"], "arguments": args if isinstance(args, dict) else {}})
            except Exception as e:
                errors.append("could not parse a tool call: %s" % e)
    else:
        text = body
        _, calls, text, errors = parse(text)        # JSON-in-a-code-fence fallback
    text = re.sub(r"<\|?/?(im_start|im_end|endoftext|eot_id|end_of_turn)\|?>", "", SPECIAL_RE.sub("", text)).strip()
    return thinking, calls, text, errors


# ── models with no tool-calling support at all ───────────────────────────
PROMPT_TOOLS_HEADER = """

# Tools
To use a tool, reply with ONLY a JSON object inside a ```json code block, like this:
```json
{"name": "tool_name", "arguments": {"key": "value"}}
```
Use one tool per reply and wait for its result. When you have the final answer, reply in plain text without a code block.
Available tools:
"""


def tools_as_text(tools):
    lines = []
    for t in tools or []:
        f = t["function"]
        props = f["parameters"].get("properties", {})
        req = set(f["parameters"].get("required", []))
        args = ", ".join("%s%s: %s" % (k, "" if k in req else "?", v.get("type", "string")) for k, v in props.items())
        lines.append("- %s(%s) — %s" % (f["name"], args, f["description"]))
    return PROMPT_TOOLS_HEADER + "\n".join(lines)


def flatten_for_prompt_tools(messages, tools):
    """Rewrite a native tool-calling transcript into plain system/user/assistant text."""
    out = []
    for m in messages:
        if m["role"] == "system":
            out.append({"role": "system", "content": m["content"] + (tools_as_text(tools) if tools else "")})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            c = m["tool_calls"][0]["function"]
            out.append({"role": "assistant", "content": (m.get("content") or "") + "\n```json\n%s\n```" % json.dumps(
                {"name": c["name"], "arguments": c["arguments"]}, ensure_ascii=False)})
        elif m["role"] == "tool":
            body = "[result of %s]\n%s" % (m.get("name", "tool"), m["content"])
            if out and out[-1]["role"] == "user":
                out[-1]["content"] += "\n\n" + body
            else:
                out.append({"role": "user", "content": body})
        else:
            if out and out[-1]["role"] == m["role"] == "user":
                out[-1]["content"] += "\n\n" + m["content"]
            else:
                out.append({"role": m["role"], "content": m["content"]})
    if tools and not any(m["role"] == "system" for m in out):
        out.insert(0, {"role": "system", "content": tools_as_text(tools).strip()})
    return out
