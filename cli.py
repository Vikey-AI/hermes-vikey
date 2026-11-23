"""`hermes vikey ...` (Hermes loads this file for the active memory provider, without the provider module)."""

from __future__ import annotations

from . import client
from .store import NoteStore


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def register_cli(parser) -> None:
    sub = parser.add_subparsers(dest="vikey_command")
    sub.add_parser("login", help="Login dengan Vikey")
    sub.add_parser("logout", help="Hapus login Vikey dari komputer ini")
    sub.add_parser("status", help="Akun, modul aktif, dan jumlah catatan")
    notes = sub.add_parser("notes", help="Catatan Memori Pintar terbaru")
    notes.add_argument("--all", action="store_true", help="Termasuk yang diarsipkan dan usang")


def vikey_command(args) -> int:
    cmd = getattr(args, "vikey_command", None) or "status"
    if cmd == "login":
        ok, message = client.login()
        print(message)
        return 0 if ok else 1
    if cmd == "logout":
        client.forget_token()
        print("Login Vikey dihapus dari komputer ini. Cabut perangkatnya juga di halaman Perangkat kalau perlu.")
        return 0
    if cmd == "notes":
        store = NoteStore()
        statuses = ("active", "builtin", "superseded", "archived") if getattr(args, "all", False) else ("active", "builtin")
        for row in store.recent(limit=30, statuses=statuses):
            print(f"{row['id']}  {row['status']:<10}  {_clip(row['text'].replace(chr(10), ' '), 100)}")
        return 0
    print(f"Server   {client.base_url()}")
    if not client.token():
        print("Login    belum. Jalankan: hermes vikey login")
        return 1
    try:
        me = client.request("GET", "/v1/me")
        user = me.get("user") or {}
        print(f"Login    {user.get('name') or user.get('email') or 'akun Vikey'}")
        for key, mod in (me.get("modules") or {}).items():
            print(f"Modul    {mod.get('name', key)}: {'aktif' if mod.get('active') else mod.get('message') or 'tidak aktif'}")
    except client.ApiError as e:
        print(f"Login    {e}")
    counts = NoteStore().count()
    print("Catatan  " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "belum ada"))
    return 0
