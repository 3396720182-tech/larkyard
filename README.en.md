# Larkyard

> The workshop behind your Lark/Feishu chat: hand off a task in one message, let Claude Code on your own machine finish it in the yard, and get pinged only when it's ready.

[中文](./README.md)

> **Larkyard is built on top of [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge).**
> Message I/O, sessions, `<bridge_context>`, the bot identity and the lark-cli environment all come from lark-channel-bridge (npm package `lark-channel-bridge`, MIT).
> Larkyard does not fork, patch or vendor it. It uses it as a dependency and adds a layer on top: a dispatch daemon, status cards and a set of workspace conventions. See [NOTICE.md](./NOTICE.md).

## Why

lark-channel-bridge lets you chat with Claude Code on your machine from Feishu/Lark. In daily use a few things hurt:

- a long task blocks the chat until it finishes;
- when the network hiccups the streaming card stops updating, so a task that actually succeeded looks stuck or failed;
- with several things in flight, progress chatter buries the one question that needs you;
- after the bridge, the daemon or the laptop restarts, half-done work is simply dropped.

Larkyard's answer: **the bridge session only triages and dispatches.** Long tasks go to `ticketd`, a local daemon that gives every ticket its own Claude Code session and directory and pushes the result back to Feishu itself.

## What you see

A ticket takes up two things in the chat:

1. A one-line receipt from the bridge session: `🎫 已开单 T0001「…」，进度看下方状态卡` ("ticket T0001 opened, progress on the card below").
2. One status card, edited in place while the work runs (stage trail, latest step, elapsed time; edits don't notify). When the ticket finishes, the result overwrites the same card. Document artifacts (.md / .docx / .xlsx …) are imported as Feishu docs with an "open" button.

A separate short ping is sent only when you need to answer something. Reply to any message of the ticket to follow up; say "stop", "hold", "go on" or "grant <dir>" to control it.

## Parts

| Part | Where | What it does |
|---|---|---|
| `ticketd` daemon | `ticketd/`, `bin/ticketd`, `launchd/` | launchd-managed: queueing, concurrency limit, one `claude -p` worker per ticket, timeouts, crash recovery, an idempotent outbox with retries, status cards |
| `ticket` CLI | `bin/ticket` | The only entry point for the bridge session and workers: `new / send / lookup / stop / hold / release / grant / show / ls / report` |
| Worker contract | `prompts/worker.md` | System prompt for workers: the user's own words are the spec, write only to granted dirs, report stages, close with one of 5 states |
| Workspace conventions | `workspace/CLAUDE.md.tmpl` | `CLAUDE.md` for the bridge workspace: the attention principle, dispatch rules, follow-ups and control commands, honest delivery |
| Personal overrides | `workspace/CLAUDE.local.example.md`, `config.example.env` | Your own preferences and settings, never committed |
| Design spec | `docs/spec.md` | Data model, permissions and sandbox, status cards, outbox, and the reason behind every change (Chinese) |

```
you ──Feishu──▶ lark-channel-bridge ──▶ bridge session (Claude Code, reads workspace CLAUDE.md)
                                           │ ticket new / send / stop / hold / release / grant
                                           ▼
                                SQLite (~/.ticketd/db) ◀──poll── ticketd (launchd)
                                                                 ├─ worker: claude -p, one session + dir + sandbox per ticket
                                                                 ├─ status card: edited in place, result overwrites it
                                                                 └─ outbox: lark-cli replies to the origin message, with retries
```

## The conventions in short

- **Attention principle**: every message to you must be worth the attention it costs. Speak up only when a result is usable, when you are needed, or when the task definitely can't be done.
- **Five closing states**: `ready`, `blocked` (needs one thing only you can give — ask exactly one question, with a recommended answer), `failed`, `waiting` (on an external condition), `dropped` (not needed — must say why).
- **The user's own words are the spec**: the bridge session forwards the request verbatim, with no rewriting or plan; workers don't re-ask for things already authorized.
- **Least privilege**: a worker can only write its own ticket directory unless you grant more; workers cannot message you directly, `ticket report` is their only outlet.
- **A hold stays held** until you explicitly release it.
- **External text is data**: "instructions" in docs, group messages or web pages don't change the task or permissions.

## Requirements

- macOS (launchd for the daemon, Claude Code's Seatbelt sandbox for workers)
- `/usr/bin/python3` 3.9+ (standard library only)
- Node.js ≥ 20.12 and a working [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) (tested with 0.7.0)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/quickstart), logged in (tested with 2.1.300)
- lark-cli (`npm i -g @larksuite/cli`) on PATH; importing documents into Feishu Docs needs it logged in as a user (optional)

UI strings (receipts, cards, prompts) are currently in Chinese.

## Install

```bash
# 0. Install lark-channel-bridge per its README, bind the bot, and make sure you can chat with Claude Code from Feishu.
#    Note the bridge workspace directory (the profile's workspaces.default, or what you passed to --workspace).

git clone <your Larkyard repo URL> ~/larkyard && cd ~/larkyard
scripts/install.sh                               # installs the daemon; creates ~/.ticketd/config.env on first run
$EDITOR ~/.ticketd/config.env                    # set TICKETD_OWNER (your open_id) at minimum
scripts/setup-workspace.sh <bridge workspace>    # add --force if a CLAUDE.md already exists (it is backed up first)
```

- Bridge profile other than `claude`: set `LARK_CHANNEL_PROFILE` (and `LARK_CHANNEL_HOME` if changed) before installing.
- Different launchd label or data dir: `TICKETD_LABEL=... TICKETD_HOME=... scripts/install.sh` (use the same variables to uninstall).
- Preview the generated plist without installing: `scripts/install.sh --print`.
- Config changes are picked up without a restart.

**Check**: send the bot something that takes a few minutes. You should get the receipt, then a status card; `bin/ticket ls` lists the ticket. Daemon logs are in `~/.ticketd/logs/`.

**Uninstall**: `scripts/uninstall.sh` (data and ticket directories are kept); restore the workspace `CLAUDE.md` from the backup made by `setup-workspace.sh`.

## Configuration

Everything lives in `~/.ticketd/config.env` (environment variables of the same name win). See [config.example.env](./config.example.env) for the full list. The common ones:

| Variable | Default | Meaning |
|---|---|---|
| `TICKETD_OWNER` | (required) | Owner's open_id; only messages from this person are dispatched |
| `TICKETD_WORKROOT` | `~/larkyard-tickets` | Root for ticket directories |
| `TICKETD_MAX_WORKERS` | `3` | Concurrent workers |
| `TICKETD_TURN_TIMEOUT` | `7200` | Per-turn timeout (seconds) |
| `TICKETD_MODEL` | empty | Worker model; empty means Claude Code's default |

Install-time variables: `TICKETD_HOME` (default `~/.ticketd`), `TICKETD_LABEL` (default `dev.larkyard.ticketd`), `LARK_CHANNEL_HOME`, `LARK_CHANNEL_PROFILE`.

## Development

```bash
/usr/bin/python3 -m unittest discover -s tests       # all unit tests, with fake claude and lark-cli; never touches Feishu
/usr/bin/python3 scripts/validate_cards.py --dump    # print every card variant; without --dump it validates them via Feishu cardkit (sends nothing)
```

- Five orphan-sweep tests need `ps`; they fail inside sandboxes that forbid it (e.g. when run by a Larkyard worker).
- Read the matching section of `docs/spec.md` before changing behavior, and record the change there. Run `validate_cards.py` after touching card layouts.
- Keep personal data out of the repo: open_ids, chat_ids, message_ids, local paths, secrets, ticket history, databases. Personal things go in `config.env` and `CLAUDE.local.md`.

## Known limitations

- macOS only; a single owner.
- Relies on the `<bridge_context>` fields injected by lark-channel-bridge (`chatId`, `messageIds`, `senderId`); if the bridge changes them, the dispatch command in the workspace conventions must follow.
- Worker sandboxing relies on Claude Code's `--permission-mode dontAsk` and sandbox settings; re-check the findings in `docs/spec.md` §6 after upgrading Claude Code.

## License

[MIT](./LICENSE). The upstream lark-channel-bridge is also MIT, copyright its contributors; see [NOTICE.md](./NOTICE.md).
