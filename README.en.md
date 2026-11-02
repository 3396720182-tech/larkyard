# Larkyard

![Larkyard](docs/assets/social-preview.jpg)

> The workshop behind your Lark/Feishu chat: hand off a task in one message, let **Claude Code or Codex** on your own machine finish it in the yard, and get pinged only when it's ready.

[中文](./README.md)

> **Larkyard is built on top of [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge).**
> Message I/O, sessions, `<bridge_context>`, the bot identity and the lark-cli environment all come from lark-channel-bridge (npm package `lark-channel-bridge`, MIT).
> Larkyard does not fork, patch or vendor it. It uses it as a dependency and adds a layer on top: a dispatch daemon, status cards and a set of workspace conventions. See [NOTICE.md](./NOTICE.md).

**Two kinds of worker, switched with one phrase**: each ticket can be run by **Claude Code** or the **Codex CLI**. Say "use codex to …" in Feishu and that ticket goes to Codex; otherwise it goes to the default executor (Claude Code unless you configure Codex). Both share the same queueing, status cards, permission boundaries, follow-ups and result delivery. See [Routing: Claude Code or Codex](#routing-claude-code-or-codex).

## Why

lark-channel-bridge lets you chat with an AI coding agent on your machine from Feishu/Lark. In daily use a few things hurt:

- a long task blocks the chat until it finishes;
- when the network hiccups the streaming card stops updating, so a task that actually succeeded looks stuck or failed;
- with several things in flight, progress chatter buries the one question that needs you;
- after the bridge, the daemon or the laptop restarts, half-done work is simply dropped.

Larkyard's answer: **the bridge session only triages and dispatches.** Long tasks go to `ticketd`, a local daemon that gives every ticket its own worker session (Claude Code or Codex, routed per ticket) and directory and pushes the result back to Feishu itself.

## What you see

A ticket takes up two things in the chat:

1. A one-line receipt from the bridge session: `🎫 已开单 T0001「…」，进度看下方状态卡` ("ticket T0001 opened, progress on the card below").
2. One status card, edited in place while the work runs (stage trail, latest step, elapsed time; edits don't notify). When the ticket finishes, the result overwrites the same card. Document artifacts (.md / .docx / .xlsx …) are imported as Feishu docs with an "open" button.

A separate short ping is sent only when you need to answer something. Reply to any message of the ticket to follow up; say "stop", "hold", "go on" or "grant <dir>" to control it.

## Routing: Claude Code or Codex

| What you say in Feishu | Who does it |
|---|---|
| "Tidy up X" (no executor named) | The default executor: `TICKETD_EXECUTOR`, which is **Claude Code** unless you change it |
| "Use codex to tidy up X" / "give this one to codex" | **Codex**: the bridge session adds `--executor codex` when dispatching |
| A follow-up quoting one of the ticket's messages | The ticket's original executor, resuming the same session |

- **You decide the route**: the bridge session only checks whether you explicitly named codex. It never guesses which executor suits a task, so routing stays stable. From a terminal you can also run `bin/ticket new … --executor claude|codex`.
- **The executor is fixed when the ticket is created**: Claude Code and Codex sessions can't be moved between each other. Open a new ticket to switch. `ticket show` prints the executor and the terminal command to take the ticket over.
- **Codex for everything**: put `TICKETD_EXECUTOR=codex` in `config.env` and tickets that don't name an executor go to Codex as well.

The two workers side by side:

| | Claude Code worker | Codex worker |
|---|---|---|
| How it runs | `claude -p --session-id/--resume …` (stream-json) | `codex exec --json …` / `codex exec resume <thread_id> …` |
| System prompt | `prompts/worker.md` via `--append-system-prompt` | `worker.md` + `worker.codex.md` via `developer_instructions` |
| Sandbox | Claude Code's Seatbelt sandbox + permission rules (`dontAsk`) | Codex's own Seatbelt sandbox (`workspace-write`, `approval_policy=never`) |
| Writable | Ticket dir, granted dirs, temp dirs, lark-cli config dirs | Same; `.git` in granted repos is writable by default (`TICKETD_CODEX_GIT_WRITABLE=1`), so workers can `git commit` |
| No messaging you directly | Permission deny rules block `lark-cli im +messages-send` and similar | execpolicy rules in a dedicated `CODEX_HOME` block the same commands plus all `lark-cli api` writes (rules match whole words only, so they are stricter) |
| "Latest step" on the card | The description of each command (e.g. "run all unit tests") | Program name only (e.g. "运行 pytest"), so the worker reports stages more often |
| Verified on | Claude Code 2.1.300 | codex-cli 0.134.0 |

A few details about Codex workers:
- **Give ticketd its own `CODEX_HOME`**: set `TICKETD_CODEX_HOME` to a directory containing a working `config.toml`. Each time ticketd starts a Codex worker it writes the anti-messaging rules there. Without it everything still runs, just without that guardrail.
- **A predictable worker environment**: ticketd disables Codex's shell snapshot, so your login shell's environment isn't replayed, and keeps variables whose names contain `TOKEN` / `KEY` / `SECRET` out of the worker's shell.
- Commands rejected by the rules don't appear in the process log. Measured behaviour behind every flag: `docs/spec.md` §5.2.1 and §6 (Chinese).

## Parts

| Part | Where | What it does |
|---|---|---|
| `ticketd` daemon | `ticketd/`, `bin/ticketd`, `launchd/` | launchd-managed: queueing, concurrency limit, one worker per ticket (`claude -p` by default, `codex exec` per ticket), timeouts, crash recovery, an idempotent outbox with retries, status cards |
| `ticket` CLI | `bin/ticket` | The only entry point for the bridge session and workers: `new / send / lookup / stop / hold / release / grant / show / ls / report` |
| Worker contract | `prompts/worker.md`, `prompts/worker.codex.md` | System prompt for workers: the user's own words are the spec, write only to granted dirs, report stages, close with one of 5 states; Codex workers also get `worker.codex.md` |
| Executors | `ticketd/executors.py` | Separates "which agent CLI runs a turn" from the scheduler: one implementation for Claude Code and one for Codex; Codex events are translated into the same shape, so status cards, stall detection and retries are shared |
| Workspace conventions | `workspace/CLAUDE.md.tmpl` | `CLAUDE.md` for the bridge workspace: the attention principle, dispatch rules, follow-ups and control commands, honest delivery |
| Personal overrides | `workspace/CLAUDE.local.example.md`, `config.example.env` | Your own preferences and settings, never committed |
| Design spec | `docs/spec.md` | Data model, permissions and sandbox, status cards, outbox, and the reason behind every change (Chinese) |

```
you ──Feishu──▶ lark-channel-bridge ──▶ bridge session (Claude Code, reads workspace CLAUDE.md)
                                           │ ticket new / send / stop / hold / release / grant
                                           ▼
                                SQLite (~/.ticketd/db) ◀──poll── ticketd (launchd)
                                                                 ├─ worker: Claude Code (claude -p) or Codex (codex exec),
                                                                 │         routed per ticket, one session + dir + sandbox each
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

- macOS (launchd for the daemon; Claude Code's or Codex's own Seatbelt sandbox for workers)
- `/usr/bin/python3` 3.9+ (standard library only)
- Node.js ≥ 20.12 and a working [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) (tested with 0.7.0)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/quickstart), logged in (tested with 2.1.300). Required: the bridge session and the default worker use it
- lark-cli (`npm i -g @larksuite/cli`) on PATH; importing documents into Feishu Docs needs it logged in as a user (optional)
- [Codex CLI](https://github.com/openai/codex) (`codex` logged in or with a provider configured; verified on codex-cli 0.134.0). Optional: only needed to route tickets to Codex. Install it before running `scripts/install.sh`, which adds it to the daemon's PATH

UI strings (receipts, cards, prompts) are currently in Chinese.

## Install

```bash
# 0. Install lark-channel-bridge per its README, bind the bot, and make sure you can chat with Claude Code from Feishu.
#    Note the bridge workspace directory (the profile's workspaces.default, or what you passed to --workspace).

git clone https://github.com/3396720182-tech/larkyard.git ~/larkyard && cd ~/larkyard
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
| `TICKETD_MODEL` | empty | Model for Claude Code workers; empty means Claude Code's default |
| `TICKETD_EXECUTOR` | `claude` | Default executor for new tickets: `claude` or `codex` (override per ticket with `ticket new --executor`) |
| `TICKETD_CODEX` | `codex` | Codex CLI path. launchd's PATH is generated by `install.sh` and only contains the directories of node, lark-cli, claude (and codex, if found at install time); use an absolute path otherwise |
| `TICKETD_CODEX_MODEL` | empty | Codex worker model; empty means the default in `config.toml` |
| `TICKETD_CODEX_HOME` | empty | Dedicated `CODEX_HOME` for ticketd (holds the anti-messaging execpolicy rules); empty means `~/.codex` and no rules are written |
| `TICKETD_CODEX_GIT_WRITABLE` | `1` | Whether Codex workers may `git commit` in granted repos; `0` keeps Codex's built-in read-only `.git` protection |

Install-time variables: `TICKETD_HOME` (default `~/.ticketd`), `TICKETD_LABEL` (default `dev.larkyard.ticketd`), `LARK_CHANNEL_HOME`, `LARK_CHANNEL_PROFILE`.

## Development

```bash
/usr/bin/python3 -m unittest discover -s tests       # all unit tests, with fake claude / codex / lark-cli; never touches Feishu
/usr/bin/python3 scripts/validate_cards.py --dump    # print every card variant; without --dump it validates them via Feishu cardkit (sends nothing)
```

- Five orphan-sweep tests need `ps`; they fail inside sandboxes that forbid it (e.g. when run by a Larkyard worker).
- Read the matching section of `docs/spec.md` before changing behavior, and record the change there. Run `validate_cards.py` after touching card layouts.
- Keep personal data out of the repo: open_ids, chat_ids, message_ids, local paths, secrets, ticket history, databases. Personal things go in `config.env` and `CLAUDE.local.md`.

## Known limitations

- macOS only; a single owner.
- Relies on the `<bridge_context>` fields injected by lark-channel-bridge (`chatId`, `messageIds`, `senderId`); if the bridge changes them, the dispatch command in the workspace conventions must follow.
- The bridge session (the one that triages and dispatches) has only been written for and tested on Claude Code: the workspace conventions are a `CLAUDE.md`. A Codex bridge session (profile `agentKind: "codex"`) is untested; what can be routed to Codex is the worker.
- Worker sandboxing relies on Claude Code's `--permission-mode dontAsk` and sandbox settings; re-check the findings in `docs/spec.md` §6 after upgrading Claude Code.
- The Codex executor relies on the `codex exec --json` event format and config keys (measured on 0.134.0; real output is kept in `tests/fixtures/`). After upgrading Codex, run `tests/test_codex.py` first and re-check the sandbox per spec §6.

## License

[MIT](./LICENSE). The upstream lark-channel-bridge is also MIT, copyright its contributors; see [NOTICE.md](./NOTICE.md).
