"""
mcp_client.py — plug external tool servers into Turner (Model Context Protocol).

Servers are listed in mcp.json next to turner.py:

    {"servers": {
        "demo": {"command": "python", "args": ["examples/mcp_demo_server.py"],
                 "skills": ["chat", "analyse"], "trust": false}
    }}

Each server's tools appear to the model as  <server>__<tool>.  They run in a
separate process that Turner starts and stops. Unless "trust" is true, every
call asks for approval, because Turner cannot know what an external tool does.
"""

import asyncio
import json
import os
import sys
import threading

from .safety import Denied
from .tools import UNTRUSTED

MAX_TOOLS_PER_SERVER = 8          # small models choose badly from long tool lists


class MCPHub:
    def __init__(self, config_path, policy):
        self.policy = policy
        self.servers = {}          # name -> {"cfg", "session", "stop", "tools": [(tool_name, spec)]}
        self.problems = []
        self.loop = None
        self.config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    self.config = json.load(f).get("servers", {})
            except ValueError as e:
                self.problems.append("mcp.json is not valid JSON: %s" % e)
        self.base = os.path.dirname(os.path.abspath(config_path))

    def start(self):
        if not self.config:
            return
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        for name, cfg in self.config.items():
            if cfg.get("disabled"):
                continue
            try:
                fut = asyncio.run_coroutine_threadsafe(self._connect(name, cfg), self.loop)
                fut.result(timeout=cfg.get("startup_timeout", 20))
            except Exception as e:
                self.problems.append("MCP server '%s' did not start: %s: %s" % (name, type(e).__name__, str(e)[:160]))

    async def _connect(self, name, cfg):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        command = cfg["command"]
        if command in ("python", "python3"):
            command = sys.executable
        env = dict(self.policy.child_env(), **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        params = StdioServerParameters(command=command, args=[str(a) for a in cfg.get("args", [])], env=env,
                                       cwd=os.path.expanduser(cfg.get("cwd", self.base)))
        ready, stop = asyncio.Event(), asyncio.Event()
        entry = {"cfg": cfg, "session": None, "stop": stop, "tools": [], "error": None}
        self.servers[name] = entry

        async def runner():
            try:
                with open(os.devnull, "w") as quiet:
                    async with stdio_client(params, errlog=quiet) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            entry["session"] = session
                            for t in listed.tools[:MAX_TOOLS_PER_SERVER]:
                                entry["tools"].append((t.name, t.description or t.name, t.inputSchema or {}))
                            ready.set()
                            await stop.wait()
            except Exception as e:                   # noqa: BLE001 — report, never crash the agent
                entry["error"] = e
                ready.set()

        entry["task"] = asyncio.ensure_future(runner())
        await asyncio.wait_for(ready.wait(), timeout=cfg.get("startup_timeout", 20))
        if entry["error"]:
            del self.servers[name]
            raise entry["error"]

    def attach(self, toolbox, skills_table):
        """Register every MCP tool with the toolbox and add it to the skills that asked for it."""
        added = []
        for name, entry in self.servers.items():
            cfg = entry["cfg"]
            wanted = cfg.get("skills", "all")
            for tool_name, desc, schema in entry["tools"]:
                full = "%s__%s" % (name, tool_name)
                schema = {"type": "object", "properties": dict((schema or {}).get("properties", {})),
                          "required": list((schema or {}).get("required", []))}
                for prop in schema["properties"].values():       # keep declarations compact
                    for k in list(prop):
                        if k not in ("type", "description", "enum", "items"):
                            prop.pop(k)
                    prop.setdefault("type", "string")
                    prop.setdefault("description", "")
                toolbox.register(full, "[%s server] %s" % (name, desc), schema,
                                 self._caller(name, tool_name), self._guard(name, full, bool(cfg.get("trust"))))
                for sk_name, sk in skills_table.items():
                    if (wanted == "all" or sk_name in wanted) and full not in sk["tools"]:
                        sk["tools"] = sk["tools"] + [full]
                added.append(full)
        return added

    def _guard(self, server, full, trusted):
        def guard(args):
            if self.policy.approve == "readonly":
                raise Denied("read-only mode: external tools are disabled")
            if not trusted:
                self.policy.confirm("external tool " + full, json.dumps(args, ensure_ascii=False, indent=1),
                                    "it runs in the '%s' MCP server, outside Turner's control" % server)
        return guard

    def _caller(self, server, tool_name):
        def call(args):
            entry = self.servers.get(server)
            if not entry or not entry["session"]:
                return "ERROR: the '%s' server is not running." % server
            fut = asyncio.run_coroutine_threadsafe(entry["session"].call_tool(tool_name, args), self.loop)
            try:
                res = fut.result(timeout=entry["cfg"].get("timeout", 60))
            except Exception as e:
                fut.cancel()
                return "ERROR: %s call failed: %s" % (tool_name, type(e).__name__)
            parts = [getattr(c, "text", None) or "[%s content]" % getattr(c, "type", "non-text") for c in res.content]
            text = "\n".join(parts) or "(empty result)"
            return ("ERROR: " + text) if getattr(res, "isError", False) else UNTRUSTED + text
        return call

    def stop(self):
        if not self.loop:
            return
        for entry in self.servers.values():
            self.loop.call_soon_threadsafe(entry["stop"].set)
        tasks = [e.get("task") for e in self.servers.values() if e.get("task")]
        if tasks:
            async def wait_all():
                await asyncio.wait(tasks, timeout=3)
            try:
                asyncio.run_coroutine_threadsafe(wait_all(), self.loop).result(timeout=5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
