#!/usr/bin/env python3
"""
turner.py — Turner, a general-purpose local agent.

  Interactive:  ./turner
  One-shot:     ./turner "write a script that renames files by date and test it"
  Pick a mode:  ./turner --skill risk "risk assessment for migrating our file server"
  Big job:      ./turner --deep "research X, analyse data.csv against it, and write a one-page brief"
  Other model:  ./turner --model qwen3.5-4b-4bit        (see --list-models)

Modes (chosen automatically unless you pass --skill):
  code · frontend · research · write · analyse · risk · chat · plus any you add in skills/

See TURNER.md for the full guide.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from turner_core import llm as llm_mod              # noqa: E402
from turner_core import skills                      # noqa: E402
from turner_core.agent import Agent, C              # noqa: E402
from turner_core.safety import Policy               # noqa: E402

HELP = """Commands:
  /model [NAME]   show the models you can use, or switch to one (alias, folder, lmstudio, openai:MODEL@URL)
  /skill NAME     force a mode, or 'auto'.  /skills lists them
  /deep TASK      run a big job as planned steps with a sub-agent per step, then combine and review
  /review         turn the self-review pass on or off for normal answers
  /approve MODE   ask (default) · auto (never ask) · readonly (no writes, no code)
  /undo           reverse the file changes made by the last task
  /resume         continue the previous conversation in this workspace
  /think          show or hide the model's reasoning
  /clear          forget this conversation
  /memory         show what Turner remembers between sessions
  /audit          show the last tool calls
  /help  /quit"""


def print_models():
    rows = llm_mod.list_models()
    width = max([len(r[0]) for r in rows] + [10])
    for name, where, is_default, found in rows:
        print("  %s%-*s%s  %s%s%s" % (C.bold, width, name, C.off, where,
                                       "  (default)" if is_default else "", "  [found in ~/models]" if found else ""))
    print("%s  also: a folder path · a Hugging Face repo id · lmstudio[:model] · openai:MODEL@http://host:port/v1%s" % (C.dim, C.off))


def load_model(spec, quiet=False):
    if not quiet:
        print("%sloading model…%s" % (C.dim, C.off), end="\r", flush=True)
    backend = llm_mod.load_backend(spec)
    if not backend.local:
        print("%sNote: %s is not on this machine — your prompts and file contents will be sent to it.%s"
              % (C.yellow, backend.describe(), C.off))
    return backend


def build(args):
    policy = Policy(args.workdir, approve="auto" if args.yes else args.approve)
    backend = load_model(args.model, args.quiet)
    agent = Agent(backend, policy, max_steps=args.max_steps, verbose=not args.quiet,
                  show_thinking=args.show_thinking, thinking=not args.no_think, review=args.review)
    hub = None
    if os.path.exists(os.path.join(HERE, "mcp.json")):
        from turner_core.mcp_client import MCPHub
        hub = MCPHub(os.path.join(HERE, "mcp.json"), policy)
        hub.start()
        added = hub.attach(agent.toolbox, skills.SKILLS)
        if added and not args.quiet:
            print("%sexternal tools: %s%s" % (C.dim, ", ".join(added), C.off))
        for p in hub.problems:
            print("%swarning: %s%s" % (C.yellow, p, C.off))
    for name, problem in skills.load_custom(os.path.join(HERE, "skills"), set(agent.toolbox.tools)):
        print("%swarning: skill file %s skipped: %s%s" % (C.yellow, name, problem, C.off))
    return agent, policy, hub


def repl(agent, policy, forced):
    print("%sTurner%s · %s · loaded in %.1fs" % (C.bold, C.off, agent.llm.describe(), getattr(agent.llm, "load_seconds", 0)))
    print("%sworkspace: %s · approvals: %s · /help for commands%s\n" % (C.dim, policy.workdir, policy.approve, C.off))
    while True:
        try:
            line = input("%sYou:%s " % (C.bold, C.off)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        deep = False
        if line.startswith("/"):
            cmd, _, raw_arg = line[1:].partition(" ")
            cmd, raw_arg = cmd.lower(), raw_arg.strip()
            arg = raw_arg.lower()
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                print(HELP)
            elif cmd in ("model", "models"):
                if not raw_arg:
                    print("current: " + agent.llm.describe())
                    print_models()
                else:
                    try:
                        llm_mod.spec_to_config(raw_arg)             # validate before unloading the current model
                        old = agent.llm
                        old.close()
                        agent.llm = load_model(raw_arg)
                        print("model: %s (loaded in %.1fs)" % (agent.llm.describe(), getattr(agent.llm, "load_seconds", 0)))
                    except SystemExit as e:
                        print(str(e))
                        if getattr(agent.llm, "model", None) is None and agent.llm.kind == "mlx":
                            agent.llm = load_model(None)            # reload the default if the switch failed midway
                    except Exception as e:
                        print("could not load %s: %s" % (raw_arg, e))
                        agent.llm = load_model(None)
            elif cmd == "skills":
                for n, sk in skills.SKILLS.items():
                    print("  %-10s %s%s" % (n, sk["description"], "  [custom]" if sk.get("custom") else ""))
            elif cmd == "skill":
                if arg in skills.SKILLS:
                    forced = arg
                elif arg in ("auto", ""):
                    forced = None
                else:
                    print("unknown skill; choose from: " + ", ".join(skills.SKILLS) + ", auto")
                    continue
                print("mode: " + (forced or "auto"))
            elif cmd == "approve":
                if arg in ("ask", "auto", "readonly"):
                    policy.approve = arg
                print("approvals: " + policy.approve)
            elif cmd == "review":
                agent.review = not agent.review
                print("self-review: %s" % ("on" if agent.review else "off"))
            elif cmd == "undo":
                done = policy.undo()
                print("\n".join(done) if done else "nothing to undo")
            elif cmd == "resume":
                got = agent.resume()
                print("resumed %s" % got if got else "no earlier session in this workspace")
            elif cmd == "think":
                agent.show_thinking = not agent.show_thinking
                print("show reasoning: %s" % agent.show_thinking)
            elif cmd == "clear":
                agent.history, agent.summary, agent.last_skill = [], "", None
                print("conversation cleared")
            elif cmd == "memory":
                print(agent.memory() or "(nothing remembered yet)")
            elif cmd == "audit":
                path = os.path.join(policy.state_dir, "audit.jsonl")
                if os.path.exists(path):
                    with open(path) as f:
                        print("".join(f.readlines()[-15:]).rstrip())
                else:
                    print("(no tool calls yet)")
            elif cmd == "deep" and raw_arg:
                line, deep = raw_arg, True
            else:
                print("unknown command — /help")
                continue
            if not deep:
                continue
        if line.lower() in ("exit", "quit"):
            break
        try:
            answer = agent.run(line, skill=forced, deep=deep)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print("%serror: %s%s" % (C.red, e, C.off))
            continue
        s = agent.stats
        print("\n%sTurner:%s %s\n%s(%s · %d steps · %.0fs)%s\n" % (C.bold, C.off, answer, C.dim, s["skill"], s["steps"], s["seconds"], C.off))
    print("bye")


def main():
    ap = argparse.ArgumentParser(description="Turner — local general-purpose agent")
    ap.add_argument("task", nargs="*", help="task to run once; omit for the interactive prompt")
    ap.add_argument("--model", help="model alias, MLX folder, HF repo id, lmstudio[:model], or openai:MODEL@URL")
    ap.add_argument("--list-models", action="store_true", help="show the models Turner can use and exit")
    ap.add_argument("--skill", help="force a mode instead of auto-routing")
    ap.add_argument("--deep", action="store_true", help="plan the job as steps, run a sub-agent per step, combine and review")
    ap.add_argument("--review", action="store_true", help="add a self-review pass to normal answers (slower)")
    ap.add_argument("--resume", action="store_true", help="continue the previous conversation in this workspace")
    ap.add_argument("--workdir", default=os.environ.get("TURNER_WORKDIR", os.path.join(HERE, "workspace")),
                    help="folder Turner may write in (default: ./workspace)")
    ap.add_argument("--approve", choices=["ask", "auto", "readonly"], default="ask",
                    help="ask before risky actions (default), never ask, or read-only")
    ap.add_argument("--yes", action="store_true", help="same as --approve auto")
    ap.add_argument("--max-steps", type=int, default=12, help="tool-call budget per task or per deep step")
    ap.add_argument("--show-thinking", action="store_true", help="print the model's reasoning")
    ap.add_argument("--no-think", action="store_true", help="disable reasoning (faster, less reliable)")
    ap.add_argument("--quiet", action="store_true", help="print only the final answer")
    args = ap.parse_args()

    if args.list_models:
        print_models()
        return
    agent, policy, hub = build(args)
    try:
        if args.skill and args.skill not in skills.SKILLS:
            raise SystemExit("unknown skill %r; choose from: %s" % (args.skill, ", ".join(skills.SKILLS)))
        if args.resume:
            got = agent.resume()
            if not args.quiet:
                print("resumed %s" % got if got else "no earlier session in this workspace")
        if args.task:
            answer = agent.run(" ".join(args.task), skill=args.skill, deep=args.deep)
            if not args.quiet:
                print("\n%sTurner:%s " % (C.bold, C.off), end="")
            print(answer)
            if not args.quiet:
                s = agent.stats
                print("%s(%s · %s · %d steps · %.0fs · tools: %s)%s" % (C.dim, agent.llm.name, s["skill"], s["steps"], s["seconds"],
                                                                        ", ".join(s["tools"]) or "none", C.off))
        else:
            repl(agent, policy, args.skill)
    finally:
        if hub:
            hub.stop()


if __name__ == "__main__":
    main()
