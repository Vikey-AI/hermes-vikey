"""Satpam for Hermes: checks terminal and execute_code calls before they run.

Vikey decides. Locally there are only two shortcuts:
read-only commands skip the round trip, and when Vikey cannot be reached a small set of
clearly dangerous patterns is still refused or sent to approval. Everything else then runs,
so an outage never stops the agent.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from . import client

SAFE = {"ls", "pwd", "cat", "head", "tail", "wc", "echo", "grep", "rg", "which", "whoami", "date", "tree", "du", "df",
        "file", "stat", "uname", "diff", "sort", "uniq", "cut", "jq", "basename", "dirname", "realpath", "true"}
SAFE_GIT = re.compile(r"^git\s+(status|diff|log|show|branch(\s+(-a|-r|--list|-v+))?|remote\s+-v|rev-parse|blame|fetch|stash\s+list)\b")

OFFLINE_BLOCK = [
    re.compile(r"\brm\s+-[a-z]*(rf|fr)[a-z]*\s+(/\*?|~/?\*?|\$HOME/?\*?)\s*($|[;&|])", re.I),
    re.compile(r"\bmkfs(\.[a-z0-9]+)?\b", re.I),
    re.compile(r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|disk|hd)", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r"(\.ssh/id_|\.aws/credentials|\.env\b)[^\n;&]*\|\s*(curl|wget|nc|ncat)\b", re.I),
]
OFFLINE_ASK = [
    re.compile(r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b)", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bsudo\b"),
    re.compile(r"\b(drop\s+(database|table)|truncate\s+table)\b", re.I),
    re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", re.I),
]


def _read_only(command: str) -> bool:
    if re.search(r"[>`]|\$\(", command):
        return False
    for seg in re.split(r"&&|\|\||;|\n", command):
        for part in seg.split("|"):
            part = part.strip()
            if not part:
                continue
            if SAFE_GIT.match(part):
                continue
            if part.split()[0] not in SAFE:
                return False
    return True


_last_request: Dict[str, str] = {}


def remember_request(session_id: str, text: str) -> None:
    if text:
        _last_request[session_id or ""] = text[:1500]
        if len(_last_request) > 200:
            _last_request.pop(next(iter(_last_request)))


def _directive(verdict: str, reason: str, category: str, command: str) -> Optional[Dict[str, str]]:
    if verdict == "block":
        return {"action": "block", "message": f"Satpam Vikey memblokir perintah ini. {reason} Jelaskan ke user apa yang mau kamu lakukan dan cari cara yang lebih aman."}
    if verdict == "ask":
        return {"action": "approve", "message": f"Satpam Vikey: {reason}", "rule_key": f"vikey-satpam:{category or 'other'}"}
    return None


def on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> Optional[Dict[str, str]]:
    if tool_name not in ("terminal", "execute_code") or not isinstance(args, dict):
        return None
    command = str(args.get("command") if tool_name == "terminal" else args.get("code") or "").strip()
    if not command or not client.modules.active("satpam"):
        return None
    if tool_name == "terminal" and _read_only(command):
        return None
    try:
        res = client.request("POST", "/v1/guard/check", {
            "tool": "shell" if tool_name == "terminal" else "python",
            "command": command,
            "request": _last_request.get(session_id or "", ""),
            "cwd": str(args.get("workdir") or args.get("cwd") or ""),
        }, timeout=5)
        return _directive(res.get("verdict", "allow"), res.get("reason", ""), res.get("category", ""), command)
    except client.ApiError as e:
        if e.status == 403:
            client.modules.mark_inactive("satpam", str(e))
            return None
        # Vikey unreachable or busy: local safety net only.
        if any(p.search(command) for p in OFFLINE_BLOCK):
            return _directive("block", "Perintah ini bisa merusak data atau membocorkan kunci rahasia.", "disk", command)
        if any(p.search(command) for p in OFFLINE_ASK):
            return _directive("ask", "Perintah ini berisiko, cek dulu sebelum dijalankan.", "other", command)
        return None
