"""Local note store for Memori Pintar (SQLite + FTS5).

Nothing is ever deleted. A note changes status instead:
  active      recallable
  builtin     currently in Hermes' own MEMORY.md / USER.md (already in the prompt, so not recalled)
  superseded  a newer note made it outdated (kept, points to the newer note)
  archived    the user or agent asked to forget it (kept, can be restored)
Every change is also written to the `events` table, so the history of a note survives.

The file lives in ~/.vikey/memory.db by default, shared by every agent on this computer
(Hermes and OpenCode read the same notes). VIKEY_HOME moves the whole folder, VIKEY_MEMORY_DB
only the database.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  source TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT,
  hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_used_at REAL,
  use_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS notes_hash ON notes(hash);
CREATE INDEX IF NOT EXISTS notes_status ON notes(status, updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(text, project, content='notes', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, text, project) VALUES (new.rowid, new.text, new.project);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE OF text, project ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, text, project) VALUES ('delete', old.rowid, old.text, old.project);
  INSERT INTO notes_fts(rowid, text, project) VALUES (new.rowid, new.text, new.project);
END;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MAX_NOTE_CHARS = 1200
STOPWORDS = {
    # Indonesian and English filler that would only add noise to the full-text search.
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "untuk", "dengan", "ada", "tidak", "ga", "gak", "nggak", "ya", "kan",
    "aja", "saja", "juga", "sih", "dong", "deh", "nih", "tolong", "bisa", "mau", "apa", "gimana", "bagaimana", "kenapa",
    "saya", "aku", "kamu", "kita", "kami", "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "it",
    "this", "that", "with", "be", "can", "you", "i", "me", "my", "please", "what", "how", "why",
}


def default_path() -> Path:
    env = os.environ.get("VIKEY_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("VIKEY_HOME") or (Path.home() / ".vikey")).expanduser() / "memory.db"


def note_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def fts_query(text: str, limit_terms: int = 24) -> str:
    words = []
    for w in re.findall(r"[\w][\w.+#-]*", (text or "").lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in STOPWORDS or w in words:
            continue
        words.append(w)
        if len(words) >= limit_terms:
            break
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words)


class NoteStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        self._db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes -------------------------------------------------------------

    def _event(self, note_id: str, action: str, detail: str = "") -> None:
        self._db.execute("INSERT INTO events(note_id, action, detail, at) VALUES (?,?,?,?)", (note_id, action, detail[:2000], time.time()))

    def add(self, text: str, *, kind: str = "note", source: str = "", project: str = "", status: str = "active") -> Dict:
        """Insert a note, or return the existing one with the same text (a re-add never duplicates).
        An existing note that was archived or superseded comes back as active when added again."""
        text = (text or "").strip()[:MAX_NOTE_CHARS]
        if not text:
            raise ValueError("empty note")
        h = note_hash(text)
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT * FROM notes WHERE hash=?", (h,)).fetchone()
            if row:
                if row["status"] in ("archived", "superseded") and status == "active":
                    self._db.execute("UPDATE notes SET status='active', superseded_by=NULL, updated_at=? WHERE id=?", (now, row["id"]))
                    self._event(row["id"], "restored", source)
                    self._db.commit()
                return dict(self._db.execute("SELECT * FROM notes WHERE id=?", (row["id"],)).fetchone())
            note_id = uuid.uuid4().hex[:12]
            self._db.execute(
                "INSERT INTO notes(id, text, kind, source, project, status, hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (note_id, text, kind, source, project, status, h, now, now),
            )
            self._event(note_id, "added", f"{kind}/{source}")
            self._db.commit()
            return dict(self._db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone())

    def set_status(self, note_id: str, status: str, *, superseded_by: Optional[str] = None, detail: str = "") -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE notes SET status=?, superseded_by=?, updated_at=? WHERE id=?",
                (status, superseded_by, time.time(), note_id),
            )
            if cur.rowcount:
                self._event(note_id, status, detail or (superseded_by or ""))
            self._db.commit()
            return bool(cur.rowcount)

    def mark_used(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            now = time.time()
            self._db.executemany("UPDATE notes SET use_count=use_count+1, last_used_at=? WHERE id=?", [(now, i) for i in ids])
            self._db.commit()

    # -- reads --------------------------------------------------------------

    def get(self, note_id: str) -> Optional[Dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return dict(row) if row else None

    def by_status(self, status: str) -> List[Dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM notes WHERE status=? ORDER BY created_at", (status,))]

    def count(self) -> Dict[str, int]:
        with self._lock:
            return {r["status"]: r["n"] for r in self._db.execute("SELECT status, COUNT(*) AS n FROM notes GROUP BY status")}

    def search(self, text: str, limit: int = 20, statuses=("active",)) -> List[Dict]:
        q = fts_query(text)
        if not q:
            return []
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            try:
                rows = self._db.execute(
                    f"SELECT n.* FROM notes_fts f JOIN notes n ON n.rowid=f.rowid WHERE notes_fts MATCH ? AND n.status IN ({marks}) ORDER BY bm25(notes_fts) LIMIT ?",
                    (q, *statuses, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [dict(r) for r in rows]

    def recent(self, limit: int = 10, statuses=("active",)) -> List[Dict]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM notes WHERE status IN ({marks}) ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def candidates(self, query: str, limit: int = 30) -> List[Dict]:
        """Notes worth sending to Vikey for this message: full-text matches first,
        then the most recently used ones, without duplicates."""
        seen, out = set(), []
        for row in self.search(query, limit=limit) + self.recent(limit=10):
            if row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)
            if len(out) >= limit:
                break
        return out

    def meta_get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()
