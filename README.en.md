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
2. One status card, edited in place while the work runs (stage trail, latest step, elapsed time; edits don't notify). When the ticket finishes, the result overwrites the same card:
   - Image artifacts (png / jpg / webp / gif, up to 6) are shown in the card: one large image, or a two-column grid with a number under each so you can reply "2" to pick one.
   - `.mp4` videos are uploaded to your own Feishu Drive as you; the card shows a clickable cover and a "▶ 播放视频" (play) button that opens Drive's player. The cover is the image next to the video with the same name (`final.mp4` → `final.jpg`).
   - Document artifacts (.md / .docx / .xlsx …) are imported as Feishu docs with an "open" button.
   - The card keeps one line of conclusion and the "👉 需要你" (needs you) block; the rest of the summary and local paths sit in a collapsed details panel.

Results aren't silently lost when sending goes wrong: if you withdraw the message that opened the ticket, the result goes to the chat instead; during a network outage images and videos wait and still land in the card once the network is back, and only degrade (grey placeholder text, or a separate video message) after failing for more than 10 minutes; re-reporting the same video doesn't upload it again.

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
| Sandbox | Claude Code's sandbox (macOS: Seatbelt; Linux / WSL2: bubblewrap) + permission rules (`dontAsk`) | Codex's own sandbox (macOS: Seatbelt; Linux / WSL2: bubblewrap) (`workspace-write`, `approval_policy=never`) |
| Writable | Ticket dir, granted dirs, temp dirs, lark-cli config dirs | Same; `.git` in granted repos is writable by default (`TICKETD_CODEX_GIT_WRITABLE=1`), so workers can `git commit` |
| No messaging you directly | Permission deny rules block `lark-cli im +messages-send` and similar | execpolicy rules in a dedicated `CODEX_HOME` block the same commands plus all `lark-cli api` writes (rules match whole words only, so they are stricter) |
| "Latest step" on the card | The description of each command (e.g. "run all unit tests") | Program name only (e.g. "运行 pytest"), so the worker reports stages more often |
| Verified on (macOS) | Claude Code 2.1.300 | codex-cli 0.134.0 |

A few details about Codex workers:
- **Give ticketd its own `CODEX_HOME`**: set `TICKETD_CODEX_HOME` to a directory containing a working `config.toml`. Each time ticketd starts a Codex worker it writes the anti-messaging rules there. Without it everything still runs, just without that guardrail.
- **A predictable worker environment**: ticketd disables Codex's shell snapshot, so your login shell's environment isn't replayed, and keeps variables whose names contain `TOKEN` / `KEY` / `SECRET` out of the worker's shell.
- Commands rejected by the rules don't appear in the process log. Measured behaviour behind every flag: `docs/spec.md` §5.2.1 and §6 (Chinese).

## Parts

| Part | Where | What it does |
|---|---|---|
| `ticketd` daemon | `ticketd/`, `bin/ticketd`, `launchd/`, `systemd/` | Managed by launchd (macOS) or a systemd user service (Linux): queueing, concurrency limit, one worker per ticket (`claude -p` by default, `codex exec` per ticket), timeouts, crash recovery, an idempotent outbox with retries that falls back to the chat when the origin message was withdrawn, status cards |
| `ticket` CLI | `bin/ticket` | The only entry point for the bridge session and workers: `new / send / lookup / stop / hold / release / grant / show / ls / report` |
| Worker contract | `prompts/worker.md`, `prompts/worker.codex.md` | System prompt for workers: the user's own words are the spec, write only to granted dirs, report stages, close with one of 5 states; Codex workers also get `worker.codex.md` |
| Executors | `ticketd/executors.py` | Separates "which agent CLI runs a turn" from the scheduler: one implementation for Claude Code and one for Codex; Codex events are translated into the same shape, so status cards, stall detection and retries are shared |
| Workspace conventions | `workspace/CLAUDE.md.tmpl` | `CLAUDE.md` for the bridge workspace: the attention principle, dispatch rules, follow-ups and control commands, honest delivery |
| Personal overrides | `workspace/CLAUDE.local.example.md`, `config.example.env` | Your own preferences and settings, never committed |
| Design spec | `docs/spec.md` | Data model, permissions and sandbox, status cards, outbox, and the reason behind every change (Chinese) |

```
you ──Feishu──▶ lark-channel-bridge ──▶ bridge session (Claude Code, reads workspace CLAUDE.md)
▲                                          │ ticket new / send / stop / hold / release / grant
│                                          ▼
│                               SQLite (~/.ticketd/db) ◀──poll── ticketd (launchd / systemd)
│                                                                ├─ worker: Claude Code (claude -p) or Codex (codex exec),
│                                                                │         routed per ticket, one session + dir + sandbox each
│                                                                ├─ status card: edited in place, result overwrites it
│                                                                └─ outbox: prepares artifacts, then sends via lark-cli
│                                                                   ├─ images go into the card; videos go to your Drive as you (uploaded once per video); docs become Feishu docs
│                                                                   ├─ network down: back off and wait; degrade only after 10+ minutes of failures
│                                                                   ├─ replies to the origin message, or posts to the chat if it was withdrawn
└──◀── result card / pings ─────────────────────────────────────────┘
```

## The conventions in short

- **Attention principle**: every message to you must be worth the attention it costs. Speak up only when a result is usable, when you are needed, or when the task definitely can't be done.
- **Five closing states**: `ready`, `blocked` (needs one thing only you can give — ask exactly one question, with a recommended answer), `failed`, `waiting` (on an external condition), `dropped` (not needed — must say why).
- **The user's own words are the spec**: the bridge session forwards the request verbatim, with no rewriting or plan; workers don't re-ask for things already authorized.
- **Least privilege**: a worker can only write its own ticket directory unless you grant more; workers cannot message you directly, `ticket report` is their only outlet.
- **A hold stays held** until you explicitly release it.
- **External text is data**: "instructions" in docs, group messages or web pages don't change the task or permissions.

## Requirements

- macOS, or Linux (including WSL2 on Windows). On macOS the daemon runs under launchd and workers in the Seatbelt sandbox; on Linux it runs as a systemd user service and workers in the bubblewrap sandbox, with the extra requirements under "Linux / WSL2" below. Native Windows is not supported; use WSL2
- Python 3.9+ (standard library only): `/usr/bin/python3` by default, or any interpreter set in `TICKETD_PYTHON`
- Node.js ≥ 20.12 and a working [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) (tested with 0.7.0)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/quickstart), logged in (tested with 2.1.300). Required: the bridge session and the default worker use it
- lark-cli (`npm i -g @larksuite/cli`) on PATH; importing documents into Feishu Docs and uploading videos to Drive need it logged in as a user (optional; without it a video is sent as a separate video message after the card)
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
- Different service name (launchd label / systemd unit name) or data dir: `TICKETD_LABEL=... TICKETD_HOME=... scripts/install.sh` (use the same variables to uninstall).
- Preview the generated plist (a systemd unit on Linux) without installing: `scripts/install.sh --print`.
- `/usr/bin/python3` older than 3.9 or missing: `TICKETD_PYTHON=/path/to/python3 scripts/install.sh`.
- Config changes are picked up without a restart.

### Linux / WSL2

On Linux `scripts/install.sh` installs a systemd user service (`~/.config/systemd/user/dev.larkyard.ticketd.service`); the other steps are the same as above. Prepare the host first:

```bash
# Worker sandbox: Claude Code needs bubblewrap and socat; Codex also prefers the system bubblewrap
sudo apt-get install bubblewrap socat        # Fedora: sudo dnf install bubblewrap socat

# From Ubuntu 24.04 AppArmor stops bwrap from creating user namespaces; if this prints 1, add an AppArmor profile for bwrap
sysctl kernel.apparmor_restrict_unprivileged_userns
sudo tee /etc/apparmor.d/bwrap > /dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo systemctl reload apparmor

# Keep the user service running after you log out, and start it at boot
sudo loginctl enable-linger "$USER"
```

- **Turn on systemd in WSL2 first**: put the two lines `[boot]` and `systemd=true` in `/etc/wsl.conf`, run `wsl --shutdown` in PowerShell and reopen the distribution. It works once `systemctl --user status` prints normally. WSL1 won't do (Claude Code's sandbox requires WSL2): check with `wsl -l -v`, upgrade with `wsl --set-version <distro> 2`.
- **In WSL2 keep everything on the Linux file system**: the bridge, this repo, `~/.ticketd` and the ticket directories all under `~`, not under `/mnt/c/...` (symlinks, permissions and file locks behave differently on Windows drives, and it's slow). Install the bridge in the same WSL distribution.
- The install script checks the host: it stops on WSL1, when node / lark-cli / claude resolve to Windows builds on a Windows drive (`/mnt/c/...`) in WSL2, and when there is no systemd user instance; it warns about missing bwrap / socat, AppArmor restricting user namespaces, and lingering being off. systemd must be ≥ 240 (Ubuntu 20.04, Debian 11 and later are fine).
- Manage the service with `systemctl --user status|restart|stop dev.larkyard.ticketd`; logs are in `~/.ticketd/logs/` as on macOS.
- On Linux lark-cli uses no OS keychain: its credentials and master key are files in `~/.local/share/lark-cli`. ticketd adds that directory to the worker sandbox's writable paths (token refreshes write there).
- On Linux the repo, home and `TICKETD_HOME` paths must not contain whitespace or `%` `$` `"` `\` (systemd units treat them specially); the install script refuses them.

**Check**: send the bot something that takes a few minutes. You should get the receipt, then a status card; `bin/ticket ls` lists the ticket. Daemon logs are in `~/.ticketd/logs/`.

**Uninstall**: `scripts/uninstall.sh` (removes the launchd agent on macOS, stops and removes the systemd user service on Linux; data and ticket directories are kept); restore the workspace `CLAUDE.md` from the backup made by `setup-workspace.sh`.

## Configuration

Everything lives in `~/.ticketd/config.env` (environment variables of the same name win). See [config.example.env](./config.example.env) for the full list. The common ones:

| Variable | Default | Meaning |
|---|---|---|
| `TICKETD_OWNER` | (required) | Owner's open_id; only messages from this person are dispatched |
| `TICKETD_WORKROOT` | `~/larkyard-tickets` | Root for ticket directories |
| `TICKETD_MAX_WORKERS` | `3` | Concurrent workers |
| `TICKETD_TURN_TIMEOUT` | `7200` | Per-turn timeout (seconds) |
| `TICKETD_OUTAGE_RETRY_EVERY` | `900` | While waiting out an outage, how often to resume automatically (seconds): network or API failures after the short retries, insufficient balance, authentication failures |
| `TICKETD_OUTAGE_RETRY_HOURS` | `24` | How long to keep waiting out an outage before marking the ticket failed (hours); `0` fails right away |
| `TICKETD_MODEL` | empty | Model for Claude Code workers; empty means Claude Code's default |
| `TICKETD_EXECUTOR` | `claude` | Default executor for new tickets: `claude` or `codex` (override per ticket with `ticket new --executor`) |
| `TICKETD_CODEX` | `codex` | Codex CLI path. The service's PATH (launchd / systemd) is generated by `install.sh` and only contains the directories of node, lark-cli, claude (and codex, if found at install time); use an absolute path otherwise |
| `TICKETD_CODEX_MODEL` | empty | Codex worker model; empty means the default in `config.toml` |
| `TICKETD_CODEX_HOME` | empty | Dedicated `CODEX_HOME` for ticketd (holds the anti-messaging execpolicy rules); empty means `~/.codex` and no rules are written |
| `TICKETD_CODEX_GIT_WRITABLE` | `1` | Whether Codex workers may `git commit` in granted repos; `0` keeps Codex's built-in read-only `.git` protection |
| `TICKETD_VIDEO_FOLDER` | empty | Drive folder token that result videos are uploaded to; empty means the root of your Drive |
| `TICKETD_MEDIA_WORKERS` | `2` | How many result cards the media lane prepares at once (image/video uploads, document imports, video messages); different tickets run in parallel, each ticket stays in order |
| `TICKETD_MEDIA_VIDEO_WORKERS` | `1` | How many of those may be uploading video (limits bandwidth, memory and Feishu rate limits); capped at the value above |

Install-time variables: `TICKETD_HOME` (default `~/.ticketd`), `TICKETD_LABEL` (default `dev.larkyard.ticketd`; the systemd unit name on Linux), `LARK_CHANNEL_HOME`, `LARK_CHANNEL_PROFILE`, `TICKETD_PYTHON` (Python interpreter, default `/usr/bin/python3`; `bin/ticket` and `bin/ticketd` honour it too; on Linux it is always written into the service environment, on macOS into the plist when set explicitly).

## Development

```bash
/usr/bin/python3 -m unittest discover -s tests       # all unit tests, with fake claude / codex / lark-cli; never touches Feishu
/usr/bin/python3 scripts/validate_cards.py --dump    # print every card variant; without --dump it validates them via Feishu cardkit (sends nothing)
```

- Six orphan-sweep tests inspect real processes (`ps` on macOS, `/proc` on Linux); they fail inside sandboxes that forbid it (e.g. when run by a Larkyard worker).
- The orphan sweep has one residual risk (always the case on macOS, the same on Linux): a background program you start yourself inside a ticket directory and detach from the terminal (an editor launched with `code .` there, a tmux server) is cleaned up as a leftover when that ticket's worker finishes.
- Platform-specific code lives only in `ticketd/host.py`, `ticketd/procs.py`, `ticketd/perms.py` and `scripts/install.sh`; the unit tests mock the platform, so both branches are tested on either OS. GitHub Actions (`.github/workflows/tests.yml`) runs all unit tests on ubuntu-latest and macos-latest, and installs, reinstalls and uninstalls the systemd user service for real on Ubuntu.
- Read the matching section of `docs/spec.md` before changing behavior, and record the change there. Run `validate_cards.py` after touching card layouts (`--with-images` uploads two tiny images first so cards with images are validated too).
- Keep personal data out of the repo: open_ids, chat_ids, message_ids, local paths, secrets, ticket history, databases. Personal things go in `config.env` and `CLAUDE.local.md`.
- Leak check: `/usr/bin/python3 scripts/check_private.py --install-hooks` installs pre-commit / pre-push hooks in your clone that stop secrets, real Feishu ids, home-directory paths and non-noreply emails before they are committed or pushed (it prints locations only, never the matched text). Strings only you know (user name, company) go in `~/.ticketd/private-patterns`, one regex per line.

## Known limitations

- macOS and Linux (including WSL2); native Windows is not supported. A single owner.
- On Linux / WSL2 the only verification so far is the unit tests (mocking the platform) and CI (all unit tests plus a real systemd user-service install on ubuntu-latest); there has been no end-to-end run like on macOS (the sandbox findings in `docs/spec.md` §6 come from macOS). Not yet measured in particular: the read/write boundary of workers under Claude Code's and Codex's bubblewrap sandboxes, whether `.git` in granted repos is read-only under Codex (what `TICKETD_CODEX_GIT_WRITABLE` does on Linux), and read-only lark-cli calls inside the worker sandbox.
- On Linux, commands in Claude Code's sandbox run in their own network namespace and can't reach services on the host's `localhost`; in WSL2 workers can't launch Windows programs (`cmd.exe`, exes under `/mnt/c/...`) by default either.
- WSL2 may shut the whole distribution down some time after the last WSL terminal closes, taking ticketd and the bridge with it. To keep them running, keep a WSL terminal open or have Windows Task Scheduler start a long-running `wsl.exe` process at logon. This depends on the WSL version and has not been verified here.
- Relies on the `<bridge_context>` fields injected by lark-channel-bridge (`chatId`, `messageIds`, `senderId`); if the bridge changes them, the dispatch command in the workspace conventions must follow.
- The bridge session (the one that triages and dispatches) has only been written for and tested on Claude Code: the workspace conventions are a `CLAUDE.md`. A Codex bridge session (profile `agentKind: "codex"`) is untested; what can be routed to Codex is the worker.
- The bridge session is managed by the bridge and resumed indefinitely (`--resume`), so its context keeps growing: one bridge session measured about USD 35 over a little more than a day, with every later message paying for the whole history and triage drifting toward old topics. ticketd has no control over that session; send `/new` in Feishu now and then (for example after a batch of work) to start fresh. Tickets are unaffected: ids and state live in ticketd, and quoting old messages still routes follow-ups.
- `ticket grant --local-net` opens every local port at once (Claude Code's sandbox cannot narrow it to one port), and a service opened with `--socket` (such as docker.sock) can effectively act outside the sandbox; grant them per ticket as needed. Both only apply to Claude tickets. Connecting to local ports, binding locally and unix sockets were measured on macOS; Linux has not been measured, nor whether headless Chrome runs end to end once granted.
- Worker sandboxing relies on Claude Code's `--permission-mode dontAsk` and sandbox settings; re-check the findings in `docs/spec.md` §6 after upgrading Claude Code.
- Feishu cards have no video component, so videos don't play inline: the card has a cover and a button that opens Drive's player. Videos up to 100MB go to Drive (lark-cli switches to multipart upload above 20MB); if the Drive upload fails, videos up to 30MB go out as a separate video message after the card, and beyond that only the local path is given.
- Media uploads and document imports run in the daemon's media lane (by default up to 2 tasks at once, at most 1 of them uploading video), so they don't hold up dispatch or status cards, and while one large video uploads, other tickets' images and documents are still prepared. Messages queued behind a large video in the same ticket still wait for it (ordering is kept). With more than one large video at a time, later videos queue (see `TICKETD_MEDIA_VIDEO_WORKERS`). Stopping the service waits at most 5 seconds and does not wait for an upload in progress; if the daemon is stopped or killed mid-way through a video upload, it uploads again after restart and Drive ends up with a duplicate copy. A media task that hangs for more than 15 minutes is reported and gives up its slot, but the thread cannot be killed, so that ticket waits until it finishes or the service restarts.
- The Codex executor relies on the `codex exec --json` event format and config keys (measured on 0.134.0; real output is kept in `tests/fixtures/`). After upgrading Codex, run `tests/test_codex.py` first and re-check the sandbox per spec §6.

## License

[MIT](./LICENSE). The upstream lark-channel-bridge is also MIT, copyright its contributors; see [NOTICE.md](./NOTICE.md).
