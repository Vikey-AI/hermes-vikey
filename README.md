# Vikey for Hermes

Plugins from [Vikey](https://vikey.ai) for [Hermes Agent](https://hermes-agent.nousresearch.com), used with your Vikey account.

- **Memori Pintar (smart memory).** Hermes remembers your preferences, decisions and project context in every new session, with no limit on the number of notes. Notes stay on your computer. For each message you send, Vikey picks only the notes that matter for it, so prompts stay small.
- **Satpam (command guard).** Before Hermes runs a terminal command, Satpam checks it. Routine commands run right away, risky ones (force push, sudo, deleting data, touching production) wait for your approval, and destructive ones are refused.

Turn each plugin on or off, and see usage and billing, at [plugins.vikey.ai](https://plugins.vikey.ai).

## Install

**Let Hermes install it (works on a VPS, also over Telegram).** Sign in at [plugins.vikey.ai](https://plugins.vikey.ai), click *Buat kode pasang* and copy the message. Paste it into your chat with Hermes. Hermes runs the installer with a one-time setup code, signs in without a browser, and restarts itself so the plugin is active. The code is valid for 15 minutes and works once.

**From a terminal or SSH session**, with a setup code from the same page:

```sh
curl -fsSL https://plugins.vikey.ai/install.sh | sh -s -- --code VK-XXXX-XXXX-XXXX-XXXX
```

Without a code, the installer (macOS and Linux) installs the plugin into every supported agent on the machine and starts the login:

```sh
curl -fsSL https://plugins.vikey.ai/install.sh | sh
```

Or with the Hermes CLI:

```sh
hermes plugins install Vikey-AI/hermes-vikey
hermes config set memory.provider vikey
hermes vikey login
```

`hermes vikey login` prints a link and a code. Open the link on any device (your phone works, the server needs no browser), sign in with your Vikey account and approve the code. One login covers every agent on the computer (the Vikey plugin for OpenCode uses it too).

On a server without a browser, use a Vikey API key instead: `export VIKEY_API_KEY=...`.

## Commands

| Command | What it does |
|---|---|
| `hermes vikey status` | Account, active plugins and note counts |
| `hermes vikey notes` | Latest notes (`--all` includes archived ones) |
| `hermes vikey logout` | Remove the login from this computer |

Tools available to the agent: `vikey_memory_save`, `vikey_memory_search`, `vikey_memory_forget`.

## Your data

- Notes live in `~/.vikey/memory.db` (SQLite) on your computer. Set `VIKEY_HOME` to move the folder.
- Hermes' own `MEMORY.md` and `USER.md` are read, never written. Their entries are mirrored into the note store, so an entry you remove from them (for example to make room) is still remembered.
- Notes are never deleted. Outdated ones are archived and can be restored.
- To pick relevant notes, the plugin sends your message and a short list of candidate notes to Vikey. Vikey uses them for that decision only and does not store them.
- If Vikey cannot be reached, the plugin falls back to local search and local safety rules, so Hermes keeps working.

## Uninstall

```sh
hermes config unset memory.provider
rm -rf ~/.hermes/plugins/vikey
```

Your notes stay in `~/.vikey` until you remove that folder.
