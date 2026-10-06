# Your own modes

Each `.md` file in this folder (except this one) becomes a mode Turner can use.

```
---
name: minutes                 # the word you type after /skill or --skill
label: Meeting minutes        # shown while it works
description: one line the router uses to decide when this mode fits
tools: [read_file, read_document, write_file]   # any of Turner's tools
temperature: 0.3              # optional, default 0.5
max_tokens: 2500              # optional
triggers:                     # optional regular expressions; a match selects this mode outright
  - "\\bminutes\\b"
---
The playbook: plain instructions, ideally a short numbered procedure.
```

Tools you can list: read_file, read_document, write_file, edit_file, list_dir, search_files,
run_python, run_shell, web_search, fetch_url, risk_register, remember, plus any `server__tool` from mcp.json.
`plan` is added automatically.

Keep playbooks short and concrete. A file with a mistake is skipped with a warning at start-up.
To replace a built-in mode, use its name and add `override: true`.
