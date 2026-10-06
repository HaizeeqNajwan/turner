# Turner — local general-purpose agent

Turner is an agent written in plain Python. It runs a model on this Mac (or talks to a model server),
gives it tools, and wraps it in checks and guard rails. No agent framework is involved.

## Run it

```bash
cd ~/models/turner
./turner                                   # interactive
./turner "write csv2json.py that converts a CSV file to JSON, and test it"
./turner --skill risk "risk assessment for next month's UAV survey of WP2B"
./turner --deep "analyse sales.csv, then write a memo about the fastest-growing region and save it as memo.md"
./turner --model qwen3.5-4b "explain what parser.py does"
./turner --workdir ~/Desktop/some-project "summarise report.docx in 5 bullets"
```

`./turner` is a wrapper for `~/models/venv312/bin/python turner.py`, run under `caffeinate` so the Mac does not
idle-sleep in the middle of a task (a sleeping Mac pauses the model for as long as it sleeps).

| Flag | Meaning |
|---|---|
| `--model NAME` | which model to use (see "Models") |
| `--list-models` | show the models Turner knows about |
| `--skill NAME` | force a mode (default: chosen automatically) |
| `--deep` | plan the job as steps, run a sub-agent per step, combine and review |
| `--review` | add a self-review pass to normal answers |
| `--resume` | continue the previous conversation in this workspace |
| `--workdir DIR` | the only folder Turner may write in (default `./workspace`) |
| `--approve ask\|auto\|readonly` | ask before risky actions (default), never ask, or no writes/code at all |
| `--yes` | same as `--approve auto` |
| `--max-steps N` | tool-call budget per task, or per step in deep mode (default 12) |
| `--show-thinking` / `--no-think` | print the model's reasoning / turn reasoning off |
| `--quiet` | print only the final answer |

At the interactive prompt: `/model`, `/skill`, `/skills`, `/deep TASK`, `/review`, `/approve`, `/undo`, `/resume`,
`/think`, `/clear`, `/memory`, `/audit`, `/help`, `/quit`.

## Models

Turner talks to every model through one interface, so switching is a flag or a command, not a code change.

```bash
./turner --list-models
./turner --model qwen3.5-4b                 # an alias from models.json
./turner --model ~/models/mlx/Some-Model    # any MLX model folder
./turner --model mlx-community/Some-4bit    # a Hugging Face repo id (downloads on first use)
./turner --model lmstudio                   # whatever chat model LM Studio has loaded
./turner --model lmstudio:qwen3.8-27b       # a specific LM Studio model
./turner --model openai:MODEL@http://127.0.0.1:8080/v1    # any OpenAI-compatible server
```

Inside the prompt, `/model` lists them and `/model NAME` switches without restarting; the conversation carries over.

**`models.json`** holds the named models and the default:

```json
{
  "default": "gemma4-e2b",
  "models": {
    "gemma4-e2b": {"backend": "mlx", "path": "~/models/original/mlx-community/gemma-4-e2b-it-4bit"},
    "lmstudio":   {"backend": "openai", "base_url": "http://127.0.0.1:1234/v1", "model": "auto"}
  }
}
```

Optional keys per model: `context` (prompt-token budget), `tier` (`small` or `large`), `thinking` (`false` to disable
reasoning), `api_key_env` (name of an environment variable holding a key, for hosted servers).
MLX folders under `~/models/mlx` and `~/models/original` are also picked up automatically.
`TURNER_MODEL` sets the model for a Terminal session.

Two kinds of backend:

- **MLX, in-process** — Turner loads the weights itself. Tool-call and reasoning formats differ per model family
  (Gemma, Qwen, Mistral, …); Turner uses its own parser for Gemma 4 and mlx-lm's parsers for the others.
  A model with no tool-calling support still works: the tools are described in the prompt and calls are read as JSON.
- **Server** — LM Studio, `mlx_lm.server`, Ollama, vLLM or a hosted API. This is how to use GGUF models and
  models too big to load here. If the address is not on this machine, Turner warns that your prompts and file
  contents will be sent there.

**Tiers.** A `small` model (the default for MLX weights under about 9 GB) is kept on a short leash: the harness does
the planning and checking. A `large` model (servers, bigger weights, or `"tier": "large"`) also gets a `plan` tool
to keep its own checklist and a bigger context budget.

**Comparing models.** `evals/run_evals.py` runs the same real tasks against any model and scores them:

```bash
~/models/venv312/bin/python evals/run_evals.py --model qwen3.5-4b --repeat 2
```

## Modes

Each mode has its own checklist, tool belt and temperature (`turner_core/skills.py`).

| Mode | What it does | Tools |
|---|---|---|
| **code** | look → build → run → fix → report | list_dir, read_file, search_files, write_file, edit_file, run_python, run_shell |
| **frontend** | plan components → write each file → read every file back and fix → report | list_dir, read_file, search_files, write_file, edit_file, run_shell |
| **research** | search → open pages → cross-check → answer with sources | web_search, fetch_url, read_document, read_file, write_file |
| **write** | reader and purpose → draft → self-edit → save if asked | read_file, read_document, write_file, web_search, fetch_url |
| **analyse** | inspect → compute with pandas → interpret → limits | read_document, read_file, run_python, list_dir, search_files, write_file |
| **risk** | context → identify → score → mitigate → register | risk_register, read_document, read_file, write_file, web_search, fetch_url |
| **chat** | quick questions, arithmetic, remembering preferences | web_search, fetch_url, read_file, list_dir, run_python |

`read_document` handles .docx, .pdf, .xlsx and .csv. `remember` is available in every mode.

**Your own modes.** Any `.md` file in `skills/` becomes a mode: a few lines of settings and a plain-language playbook.
`skills/minutes.md` (meeting minutes with action items) is a working example; `skills/README.md` explains the format.

**Project instructions.** If the workspace contains `AGENTS.md`, Turner reads it at the start of every task and
follows it — the place for conventions like "British spelling", "tests live in tests/", "never touch the data folder".

**The risk register** does the arithmetic so the model can't get it wrong:
score = likelihood × impact (1–25); 1–4 Low, 5–9 Medium, 10–15 High, 16–25 Critical.
It keeps `risk_register.json`, `.md` and `.csv` in the workspace up to date after every change.

## Workflows

| Workflow | What happens | When |
|---|---|---|
| **Single agent** | route → loop of think / tool / result → checks → answer | default |
| **Deep** (orchestrator → workers) | a planner splits the job into 2–5 steps, each with a mode; every step runs as a fresh sub-agent that sees the goal, earlier results and the workspace files; the results are combined into one answer and reviewed | `--deep` or `/deep`, for jobs with several deliverables |
| **Review** (evaluator → optimiser) | a critic checks the answer against the request; if it finds problems, one revision fixes them. A revision that loses half the content is discarded | always in deep mode; `--review` or `/review` otherwise (not for code or front-end, which are checked by running and re-reading) |

## What the harness does for the model

- code that was written but never run is sent back to be run; a failed run is sent back to be fixed
- a coding task where nothing was changed or run is sent back to do the work
- front-end files that were written but not read back are sent back for review
- a research answer that never searched is sent back to search
- a risk answer that skipped the register is sent back to record the risks
- identical repeated tool calls are refused
- an answer delivered through a made-up tool (`report(...)`, `final_answer(...)`) is accepted as the answer
- old tool output is trimmed to fit the model's context budget
- long conversations are compacted: older turns become a short summary, the recent ones stay verbatim
- every turn is saved, so `--resume` or `/resume` picks the conversation up later
- when the step budget runs out, the model must say what was and wasn't finished
- if a check still fails, the answer carries a plain **Harness note**

## External tools (MCP)

Turner can use tools from Model Context Protocol servers. Copy `mcp.json.example` to `mcp.json`:

```json
{"servers": {"demo": {"command": "python", "args": ["examples/mcp_demo_server.py"],
                      "skills": "all", "trust": false}}}
```

Each server's tools appear as `server__tool` in every mode (`"skills": "all"`) or only in the modes you list
(`"skills": ["chat", "analyse"]`). Listing modes keeps tool lists short, but the tool is then only used when the
request is routed to one of them.
Unless `trust` is true, every call asks for approval. Turner takes at most 8 tools per server, because long tool
lists make small models choose badly. `examples/mcp_demo_server.py` is a tiny server you can copy.

## Guard rails (`turner_core/safety.py`)

- **Writes** only inside the workspace; an existing file is copied to `.turner/backups/` first. `/undo` reverses the
  file changes of the last task.
- **Deletes** outside the workspace are refused in every approval mode.
- **Secrets** can't be read or written: `.ssh`, `.env`, keychains, cloud credentials, `.pem` and similar.
  Reads are limited to your home folder.
- **Shell**: read-only commands run freely. Destructive ones (`sudo`, disk tools, `curl | sh`, force-push, reverse
  shells…) are always refused. Everything else asks you first — `y`, `n`, or `a` (always, for this session).
- **Python**: plain code runs; code that deletes files, starts programs, posts to the network or touches credentials
  asks first. A workspace script is vetted the same way before `python3 script.py` runs.
- **Web**: http(s) only, never localhost or private addresses; page text is marked as untrusted data.
- **External tools**: ask before each call unless the server is marked trusted; disabled in read-only mode.
- **Subprocesses** get a 60 s timeout and an environment with keys and tokens removed.
- **Audit**: every tool call is logged to `<workspace>/.turner/audit.jsonl`.
- **Memory**: `remember` refuses anything that looks like a credential.

This limits damage; it is not a sandbox. Anything you approve runs with your user's permissions.

## Limits to keep in mind

- Results depend on the model. Run the evals when you switch, and prefer a larger model for larger jobs.
- Small models vary from run to run; the same task can pass once and fail the next time.
- It can still state wrong facts. Research answers list the pages actually opened; check what matters.
- Deep mode multiplies time: each step is a full agent run.
- Front-end code is reviewed by re-reading, not compiled or opened in a browser.
- Risk likelihood and impact numbers are the model's judgement.

## Files

```
turner.py                  command line and interactive prompt
turner                     launcher using ~/models/venv312
models.json                named models and the default
mcp.json.example           template for external tool servers
skills/                    your own modes (minutes.md is an example)
turner_core/llm.py         model backends (MLX, server) and the registry
turner_core/toolcall.py    tool-call parsing for Gemma, other families, and prompt-described tools
turner_core/skills.py      built-in modes, custom skill loader, router
turner_core/tools.py       the tools
turner_core/mcp_client.py  external tool servers
turner_core/safety.py      policy, approvals, backups, undo, audit log
turner_core/agent.py       the loop, deep mode, review, compaction, sessions
tests/test_turner_core.py  offline tests (no model needed)
evals/run_evals.py         task suite for comparing models; results in evals/results/
examples/mcp_demo_server.py  a small MCP server
old/                       the earlier CrewAI experiments, unchanged
```

Run the tests: `~/models/venv312/bin/python tests/test_turner_core.py`
