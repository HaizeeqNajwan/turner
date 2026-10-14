"""
safety.py — the guard rails between the model and this Mac.

A 2B-class local model will sometimes do the wrong thing with total confidence,
so nothing it asks for is trusted blindly:

  * writes are confined to the workspace; existing files are backed up first
  * finished files can be copied to Desktop, Documents or Downloads only, and only with the user's approval
  * secrets (ssh keys, .env, keychains, cloud credentials) can't be read
  * shell commands are sorted into safe / needs-approval / always-blocked
  * python snippets that delete, spawn processes or talk to the network need approval
  * web fetches can't reach localhost or private addresses
  * every tool call is appended to an audit log

This is damage limitation, not a sandbox: an approved command runs with your
user's permissions. Keep the approval mode on "ask" unless you trust the task.
"""

import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import sys
import time
from urllib.parse import urlparse

HOME = os.path.expanduser("~")
EXPORT_DIRS = ("Desktop", "Documents", "Downloads")      # the only places a finished file may be copied to

SECRET_PATTERNS = [
    r"/\.ssh(/|$)", r"/\.aws(/|$)", r"/\.gnupg(/|$)", r"/\.kube(/|$)", r"/\.docker/config\.json$",
    r"/Library/Keychains(/|$)", r"/\.netrc$", r"/\.npmrc$", r"/\.pypirc$",
    r"(^|/)\.env(\.[\w.-]+)?$", r"\.pem$", r"\.p12$", r"\.pfx$", r"\.keychain(-db)?$",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", r"/\.claude(/|$)", r"/\.config/gh(/|$)",
    r"credentials(\.json)?$", r"\.kdbx$",
]

# Never run, whatever the approval mode.
BLOCKED_SHELL = [
    (r"\bsudo\b|\bsu\s+-|\bdoas\b", "privilege escalation"),
    (r"\brm\s+(-[\w-]*\s+)*(/|~|~/|\$HOME/?|/\*|~/\*)(\s|$)", "deleting the home or root directory"),
    (r"\brm\s+-[\w]*r[\w]*\s+(-[\w-]+\s+)*(\.\.(/|\s|$)|(/Users|/System|/Library|/Applications|/etc|/usr|/bin|/var)(/[^/\s]*)?/?(\s|$))", "recursive delete of a system or parent directory"),
    (r"\b(mkfs|diskutil\s+(erase|partition|apfs\s+delete)|fdisk|dd\s+[^|]*\bof=/dev/)", "disk formatting"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\b(shutdown|reboot|halt)\b|\bkillall\b|\bkill\s+-9\s+-?1\b|\blaunchctl\s+(unload|remove|bootout)", "stopping the system or its services"),
    (r"(curl|wget)[^|;&]*\|\s*(sudo\s+)?(ba|z|fi)?sh\b", "piping a download straight into a shell"),
    (r"\bchmod\s+(-R\s+)?777\s+(/|~)(\s|$)|\bchown\s+-R\b", "recursive permission change"),
    (r">\s*/dev/(disk|rdisk|sd)", "writing to a raw disk"),
    (r"\b(security\s+(find|dump|delete)-|defaults\s+delete|csrutil|spctl\s+--master-disable|nvram\b)", "tampering with macOS security settings"),
    (r"\bgit\s+push\b.*(--force|-f)\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[\w]*f", "destructive git operation"),
    (r"\b(crontab\s+-r|history\s+-c)\b", "wiping crontab or history"),
    (r"\b(nc|ncat|netcat)\b.*\s-e\b|/dev/tcp/|\bbash\s+-i\b", "reverse shell"),
]

# Run without asking (read-only or routine dev commands).
SAFE_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "find", "echo", "date", "which", "file",
    "stat", "du", "df", "tree", "sort", "uniq", "cut", "diff", "basename", "dirname", "whoami",
    "uname", "sw_vers", "env", "printenv", "true", "test", "cal", "md5", "shasum", "nl", "column",
}
SAFE_PREFIXES = [
    "python -m pytest", "python3 -m pytest", "pytest", "python -m py_compile", "python3 -m py_compile",
    "python --version", "python3 --version", "node --version", "npm --version", "git status",
    "git diff", "git log", "git branch", "git show", "pip list", "pip show", "pip --version",
]
SHELL_META = re.compile(r"[;&|`]|\$\(|>|<")

RISKY_PYTHON = [
    (r"\bshutil\.rmtree\b|\bos\.(remove|unlink|rmdir|removedirs)\b|\.unlink\(|\bsend2trash\b", "deletes files"),
    (r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*|kill)\b|\bpty\b", "starts other programs"),
    (r"\b(socket|smtplib|ftplib|paramiko|telnetlib)\b|requests\.(post|put|delete|patch)|urllib\.request\.urlopen\([^)]*data=", "sends data over the network"),
    (r"\bos\.(chmod|chown)\b|\bctypes\b|\bkeyring\b", "changes permissions or reaches into the system"),
    (r"\.ssh|\.aws|Keychains|\.env\b|id_rsa", "touches credentials"),
]


class Denied(Exception):
    """Raised when policy refuses an action. The message is shown to the model."""


class Policy:
    def __init__(self, workdir, approve="ask", extra_read_roots=None, interactive=None):
        self.workdir = os.path.realpath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.state_dir = os.path.join(self.workdir, ".turner")
        os.makedirs(self.state_dir, exist_ok=True)
        self.approve = approve                   # ask | auto | readonly
        self.read_roots = [os.path.realpath(p) for p in (extra_read_roots or [HOME])]
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.session = time.strftime("%Y%m%d-%H%M%S")
        self.remembered = set()                  # approvals the user granted "always" this session
        self.changes = []                        # one list per task: (path, backup-or-None) for /undo

    # ── paths ────────────────────────────────────────────────────────────
    def resolve(self, path):
        path = os.path.expanduser(str(path or "").strip())
        if not path:
            raise Denied("no path given")
        if not os.path.isabs(path):
            path = os.path.join(self.workdir, path)
        return os.path.realpath(path)

    @staticmethod
    def _inside(path, root):
        return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

    def is_secret(self, path):
        return any(re.search(p, path) for p in SECRET_PATTERNS)

    def resolve_read(self, path):
        """Like resolve, but a relative path that is not in the workspace is also looked for under the home
        folder, so "Downloads/cv.pdf" finds ~/Downloads/cv.pdf. Reading only: writes never use this."""
        raw = os.path.expanduser(str(path or "").strip())
        p = self.resolve(raw)
        if raw and not os.path.isabs(raw) and not os.path.exists(p):
            for base in (HOME, os.path.join(HOME, "Desktop"), os.path.join(HOME, "Documents"), os.path.join(HOME, "Downloads")):
                q = os.path.realpath(os.path.join(base, raw))
                if os.path.exists(q):
                    return q
        return p

    def export_roots(self):
        return [os.path.realpath(os.path.join(HOME, d)) for d in EXPORT_DIRS]

    def check_export(self, destination, filename):
        """Where may a finished file be copied? Only into Desktop, Documents or Downloads (or folders inside them)."""
        if self.approve == "readonly":
            raise Denied("read-only mode: saving files is disabled")
        raw = os.path.expanduser(str(destination or "").strip())
        if not raw:
            raise Denied("no destination given. Use Desktop, Documents or Downloads.")
        if not os.path.isabs(raw):
            raw = os.path.join(HOME, raw)
        p = os.path.realpath(raw)
        if os.path.isdir(p) or str(destination).rstrip().endswith("/"):
            p = os.path.join(p, filename)
        if self.is_secret(p):
            raise Denied("saving %s is not allowed: it looks like a credential or secret file" % p)
        if not any(self._inside(p, r) for r in self.export_roots()):
            raise Denied("saving to %s is not allowed. Copies may only go to Desktop, Documents or Downloads "
                         "(or a folder inside them)." % p)
        if not os.path.isdir(os.path.dirname(p)):
            raise Denied("the folder %s does not exist. Choose an existing folder." % os.path.dirname(p))
        return p

    def check_read(self, path):
        p = self.resolve_read(path)
        if self.is_secret(p):
            raise Denied("reading %s is not allowed: it looks like a credential or secret file" % p)
        if not any(self._inside(p, r) for r in self.read_roots + [self.workdir]):
            raise Denied("reading %s is not allowed: outside the permitted folders" % p)
        return p

    def check_write(self, path):
        if self.approve == "readonly":
            raise Denied("read-only mode: writing files is disabled")
        p = self.resolve(path)
        if self.is_secret(p):
            raise Denied("writing %s is not allowed: it looks like a credential or secret file" % p)
        if self._inside(p, self.state_dir):
            raise Denied("the .turner folder is managed by the harness; pick another path")
        if not self._inside(p, self.workdir):
            raise Denied("writing outside the workspace is not allowed. Workspace is %s — "
                         "use a relative path inside it." % self.workdir)
        return p

    def backup(self, path):
        """Copy an existing file aside before it is overwritten. Returns the backup path or None."""
        if not os.path.isfile(path):
            return None
        rel = os.path.relpath(path, self.workdir)
        self._backup_n = getattr(self, "_backup_n", 0) + 1       # counter: two saves in the same millisecond must not collide
        dest = os.path.join(self.state_dir, "backups", self.session, "%s.%d-%d" % (rel, time.time() * 1000 % 1e9, self._backup_n))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        return dest

    # ── undo ─────────────────────────────────────────────────────────────
    def begin_task(self):
        if not self.changes or self.changes[-1]:
            self.changes.append([])

    def record(self, path, backup):
        if not self.changes:
            self.changes.append([])
        self.changes[-1].append((path, backup))

    def undo(self):
        """Reverse the file changes of the most recent task that made any. Returns a list of what was done."""
        while self.changes and not self.changes[-1]:
            self.changes.pop()
        if not self.changes:
            return []
        done, first = [], {}
        for path, backup in self.changes.pop():
            first.setdefault(path, backup)          # the earliest record is the state before the task
        for path, backup in first.items():
            rel = os.path.relpath(path, self.workdir)
            if backup and os.path.exists(backup):
                shutil.copy2(backup, path)
                done.append("restored " + rel)
            elif backup is None and os.path.exists(path):
                os.remove(path)
                done.append("removed " + rel + " (it was created by that task)")
        return done

    # ── approvals ────────────────────────────────────────────────────────
    def confirm_strict(self, kind, detail, reason):
        """Like confirm, but 'auto' mode does not wave it through: used when content looks like an attack."""
        if self.approve == "auto":
            raise Denied("%s was refused: %s. Not allowed without the user at the keyboard. "
                         "Carry on with what the user actually asked for." % (kind, reason))
        saved, self.remembered = self.remembered, set()         # an earlier "always" does not cover this
        try:
            self.confirm(kind, detail, reason)
        finally:
            self.remembered = saved

    def confirm(self, kind, detail, reason):
        """Ask the user. Returns normally if allowed, raises Denied otherwise."""
        if self.approve == "auto" or kind in self.remembered:
            return
        if self.approve == "readonly":
            raise Denied("read-only mode: %s is disabled" % kind)
        if not self.interactive:
            raise Denied("%s needs the user's approval (%s) and nobody is at the keyboard. "
                         "Tell the user what you wanted to run and why." % (kind, reason))
        print("\n\033[33m┌─ approval needed: %s (%s)\033[0m" % (kind, reason))
        for line in str(detail).strip().splitlines()[:25]:
            print("\033[33m│\033[0m " + line)
        try:
            ans = input("\033[33m└─ allow? [y]es / [n]o / [a]lways this session: \033[0m").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans.startswith("a"):
            self.remembered.add(kind)
            return
        if not ans.startswith("y"):
            raise Denied("the user declined this %s. Do not retry it; explain what you wanted to do "
                         "or find another way." % kind)

    # ── shell / python / web ─────────────────────────────────────────────
    def check_shell(self, command):
        cmd = command.strip()
        if not cmd:
            raise Denied("empty command")
        for pat, why in BLOCKED_SHELL:
            if re.search(pat, cmd, re.I):
                raise Denied("blocked by safety policy (%s). This command will never be run; "
                             "do not try variations of it." % why)
        outside = self._deletes_outside(cmd)
        if outside:
            raise Denied("blocked by safety policy (deleting %s, which is outside the workspace). Turner only deletes "
                         "inside %s; do not try variations of this command." % (outside, self.workdir))
        if self.approve == "readonly":
            if not self._shell_is_safe(cmd):
                raise Denied("read-only mode: only inspection commands (ls, cat, grep, git status…) are allowed")
            return
        if not self._shell_is_safe(cmd):
            script = self._workspace_script(cmd)
            if script is not None:                # running the agent's own script: vet its source instead
                with open(script, "r", errors="replace") as f:
                    return self.check_python(f.read())
            self.confirm("shell command", cmd, "it can change files or reach the network")

    def _deletes_outside(self, cmd):
        """Return the first rm/rmdir/trash target that lies outside the workspace, if any."""
        parts = re.split(r"&&|\|\||[;|\n]", cmd)
        moved = any(re.match(r"\s*(cd|pushd)\b", p) for p in parts)
        for part in parts:
            try:
                words = shlex.split(part)
            except ValueError:
                words = part.split()
            words = [w for w in words if w not in ("command", "exec", "nohup", "time")]
            if not words or os.path.basename(words[0]) not in ("rm", "rmdir", "unlink", "trash", "srm", "shred"):
                continue
            if moved:
                return "a path after changing directory (the target cannot be verified)"
            for w in words[1:]:
                if w.startswith("-"):
                    continue
                target = os.path.realpath(os.path.join(self.workdir, os.path.expandvars(os.path.expanduser(w))))
                if "*" in w or "?" in w:
                    target = os.path.realpath(os.path.join(self.workdir, os.path.dirname(os.path.expanduser(w)) or "."))
                if not self._inside(target, self.workdir):
                    return w
        return None

    def _workspace_script(self, cmd):
        """If cmd is just `python3 some_script.py [args]` for a file inside the workspace, return its path."""
        if SHELL_META.search(cmd):
            return None
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return None
        if len(parts) < 2 or os.path.basename(parts[0]) not in ("python", "python3") or not parts[1].endswith(".py"):
            return None
        p = os.path.realpath(os.path.join(self.workdir, parts[1]))
        return p if self._inside(p, self.workdir) and os.path.isfile(p) else None

    def _shell_is_safe(self, cmd):
        if SHELL_META.search(cmd):
            return False
        if any(cmd == p or cmd.startswith(p + " ") for p in SAFE_PREFIXES):
            return True
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return False
        if not parts or os.path.basename(parts[0]) not in SAFE_COMMANDS:
            return False
        if parts[0] == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in parts):
            return False
        for a in parts[1:]:
            if a.startswith("-"):
                continue
            p = os.path.realpath(os.path.join(self.workdir, os.path.expanduser(a)))
            if self.is_secret(p):
                return False
        return True

    def check_python(self, code):
        if self.approve == "readonly":
            raise Denied("read-only mode: running code is disabled")
        reasons = [why for pat, why in RISKY_PYTHON if re.search(pat, code)]
        if reasons:
            self.confirm("python code", code, "it " + ", ".join(sorted(set(reasons))))

    def check_url(self, url):
        u = urlparse(url if "://" in url else "https://" + url)
        if u.scheme not in ("http", "https"):
            raise Denied("only http(s) URLs can be fetched")
        host = u.hostname or ""
        if not host:
            raise Denied("that is not a valid URL")
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise Denied("could not resolve host %s" % host)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise Denied("fetching local or private network addresses is not allowed")
        return u.geturl()

    def child_env(self):
        """Environment for subprocesses, with anything that looks like a secret removed."""
        env = {}
        for k, v in os.environ.items():
            if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", k, re.I):
                continue
            env[k] = v
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    # ── audit ────────────────────────────────────────────────────────────
    def audit(self, tool, args, outcome, ok):
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": self.session, "tool": tool,
               "args": {k: (v if len(str(v)) < 400 else str(v)[:400] + "…") for k, v in (args or {}).items()},
               "ok": ok, "result": str(outcome)[:300]}
        try:
            with open(os.path.join(self.state_dir, "audit.jsonl"), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
