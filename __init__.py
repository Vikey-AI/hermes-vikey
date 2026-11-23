"""Vikey for Hermes: Memori Pintar (memory provider) and Satpam (pre_tool_call guard).

Setup:
    hermes vikey login                      Login dengan Vikey (device login in the browser)
    hermes config set memory.provider vikey turn on Memori Pintar
    hermes vikey status                     account, active modules, note counts

Notes stay on this computer (~/.vikey/memory.db). For each user message the plugin finds
candidate notes locally and Vikey picks the relevant ones. Hermes' own MEMORY.md and USER.md are
read but never written: their entries are mirrored into the store, and an entry removed from
them (for example to make room) stays recallable here instead of being lost.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt, spawn_context_thread

from . import client, guard
from .store import NoteStore

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"
MAX_AUTO_NOTE = 600
LOCAL_FALLBACK = 3


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _read_entries(path: Path) -> List[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


class VikeyMemoryProvider(MemoryProvider):
    def __init__(self):
        self._store: Optional[NoteStore] = None
        self._home: Optional[Path] = None
        self._writes = True
        self._project = ""
        self._last_recall: Optional[RecallStatus] = None
        self._sync_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "vikey"

    def is_available(self) -> bool:
        return bool(client.token())

    def unavailable_reason(self) -> str:
        return "Belum login ke Vikey. Jalankan: hermes vikey login"

    def initialize(self, session_id: str, **kwargs) -> None:
        self._home = Path(kwargs.get("hermes_home") or Path.home() / ".hermes")
        self._writes = kwargs.get("agent_context", "primary") == "primary"
        cwd = kwargs.get("cwd") or kwargs.get("agent_workspace") or ""
        self._project = Path(str(cwd)).name if cwd else ""
        self._store = NoteStore()
        client.modules.refresh()
        if self._writes:
            spawn_context_thread(self._sync_builtin, name="vikey-sync-builtin").start()

    def system_prompt_block(self) -> str:
        return (
            "Memori Pintar Vikey aktif: catatan jangka panjang tersimpan tanpa batas di komputer user, dan catatan yang relevan "
            "ikut dikirim bersama pesan user. Kalau memori bawaan penuh atau ada hal penting yang harus diingat lama, simpan dengan "
            "vikey_memory_save. Cari catatan lama dengan vikey_memory_search."
        )

    # -- recall ---------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = None
        guard.remember_request(session_id, query)
        if not self._store or is_trivial_prompt(query) or not client.modules.active("memori"):
            return ""
        candidates = self._store.candidates(query, limit=30)
        if not candidates:
            return ""
        chosen: List[Dict[str, Any]] = []
        try:
            res = client.request("POST", "/v1/memory/recall", {
                "query": query,
                "memories": [{"id": c["id"], "text": self._label(c)} for c in candidates],
            }, timeout=5)
            by_id = {c["id"]: c for c in candidates}
            chosen = [by_id[s["id"]] for s in res.get("selected", []) if s.get("id") in by_id]
        except client.ApiError as e:
            if e.status == 403:
                client.modules.mark_inactive("memori", str(e))
                return ""
            # Vikey unreachable or busy: the best full-text matches only.
            chosen = self._store.search(query, limit=LOCAL_FALLBACK)
        if not chosen:
            return ""
        self._store.mark_used(c["id"] for c in chosen)
        self._last_recall = RecallStatus(provider_label="Vikey", count=len(chosen))
        lines = "\n".join(f"- {self._label(c)}" for c in chosen)
        return f"Catatan Memori Pintar Vikey yang relevan:\n{lines}"

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    @staticmethod
    def _label(note: Dict[str, Any]) -> str:
        project = note.get("project") or ""
        return f"[{project}] {note['text']}" if project else note["text"]

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        guard.remember_request(str(kwargs.get("session_id") or ""), message or "")

    # -- capture --------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages=None, turn_author=None) -> None:
        text = (user_content or "").strip()
        if not self._writes or not self._store or len(text) < 12 or is_trivial_prompt(text):
            return
        if text.startswith("<") or "<memory-context>" in text:
            return
        if not client.modules.active("memori"):
            return
        # Hermes already runs sync_turn on its background memory worker (drained at exit).
        self._capture(text)

    def _capture(self, text: str) -> None:
        try:
            similar = self._store.search(text, limit=8, statuses=("active", "builtin"))
            res = client.request("POST", "/v1/memory/remember", {
                "message": text,
                "similar": [{"id": s["id"], "text": s["text"]} for s in similar],
            }, timeout=8)
            if res.get("duplicateOf"):
                self._store.mark_used([res["duplicateOf"]])
            if not res.get("remember"):
                return
            note = self._store.add(_clip(text, MAX_AUTO_NOTE), kind="auto", source="hermes", project=self._project)
            for old_id in res.get("replaces") or []:
                old = self._store.get(old_id)
                if old and old["status"] == "active" and old_id != note["id"]:
                    self._store.set_status(old_id, "superseded", superseded_by=note["id"])
        except client.ApiError as e:
            if e.status == 403:
                client.modules.mark_inactive("memori", str(e))
        except Exception as e:  # never break the agent loop
            logger.debug("vikey capture failed: %s", e)

    # -- Hermes' own memory files ------------------------------------------------

    def _sync_builtin(self) -> None:
        """Mirror MEMORY.md / USER.md into the store (read only). Entries no longer in the files
        become active notes, so nothing the user once had in memory is lost."""
        if not self._store or not self._home:
            return
        with self._sync_lock:
            try:
                present = set()
                for target, fname in (("memory", "MEMORY.md"), ("user", "USER.md")):
                    for entry in _read_entries(self._home / "memories" / fname):
                        note = self._store.add(entry, kind=f"hermes-{target}", source="hermes", status="builtin")
                        present.add(note["id"])
                        if note["status"] != "builtin":
                            self._store.set_status(note["id"], "builtin", detail="present in built-in memory")
                for note in self._store.by_status("builtin"):
                    if note["id"] not in present:
                        self._store.set_status(note["id"], "active", detail="removed from built-in memory, kept here")
            except Exception as e:
                logger.debug("vikey builtin sync failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self._writes or not self._store:
            return
        metadata = metadata or {}
        try:
            if action in ("add", "replace") and content.strip():
                note = self._store.add(content, kind=f"hermes-{target}", source="hermes", status="builtin")
                if note["status"] != "builtin":
                    self._store.set_status(note["id"], "builtin", detail="written to built-in memory")
            # For replace and remove the files are the truth: resync decides what moved out.
            if action in ("replace", "remove"):
                spawn_context_thread(self._sync_builtin, name="vikey-sync-builtin").start()
        except Exception as e:
            logger.debug("vikey on_memory_write failed: %s", e)

    # -- tools ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "vikey_memory_save",
                "description": "Simpan catatan jangka panjang (preferensi, fakta proyek, keputusan) di Memori Pintar Vikey. Tidak ada batas jumlah, catatan yang relevan otomatis dibawa di pesan berikutnya.",
                "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "Isi catatan, satu fakta per catatan."}}, "required": ["text"]},
            },
            {
                "name": "vikey_memory_search",
                "description": "Cari catatan di Memori Pintar Vikey, termasuk catatan lama yang sudah dikeluarkan dari memori bawaan.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            {
                "name": "vikey_memory_forget",
                "description": "Arsipkan catatan yang salah atau sudah tidak berlaku. Catatan tidak dihapus dan bisa dipulihkan.",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._store:
            return json.dumps({"error": "Memori Pintar belum siap."})
        try:
            if tool_name == "vikey_memory_save":
                note = self._store.add(str(args.get("text") or ""), kind="manual", source="hermes", project=self._project)
                return json.dumps({"success": True, "id": note["id"], "status": note["status"]})
            if tool_name == "vikey_memory_search":
                rows = self._store.search(str(args.get("query") or ""), limit=10, statuses=("active", "builtin", "superseded", "archived"))
                return json.dumps({"notes": [{"id": r["id"], "status": r["status"], "text": r["text"]} for r in rows]}, ensure_ascii=False)
            if tool_name == "vikey_memory_forget":
                ok = self._store.set_status(str(args.get("id") or ""), "archived", detail="forget tool")
                return json.dumps({"success": ok})
        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"unknown tool {tool_name}"})

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        return None

    def shutdown(self) -> None:
        if self._store:
            self._store.close()
            self._store = None


def register(ctx) -> None:
    ctx.register_memory_provider(VikeyMemoryProvider())
    # Hermes loads this plugin as the memory provider (memory.provider: vikey); hooks registered
    # here ride along with it, so Satpam works whenever the plugin is the active provider.
    ctx.register_hook("pre_tool_call", guard.on_pre_tool_call)
