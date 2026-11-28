import { spawn } from 'node:child_process';
import type { LarkChannel, NormalizedMessage } from '@larksuite/channel';
import { log } from '../core/logger';

export type Intent = 'stop' | 'hold' | 'continue' | 'approve' | 'restart' | 'followup';

const CONTROL_WORDS: Record<Exclude<Intent, 'followup'>, readonly string[]> = {
  stop: ['停', '停止', '不做了', '别做了'],
  hold: ['先别动', '暂停', '等我确认'],
  continue: ['继续', '可以了', '恢复', '接着做'],
  approve: ['执行', '批准', '可以执行'],
  restart: ['换新会话重来', '会话坏了重开'],
};

/** Strip a leading @mention and surrounding whitespace / punctuation. */
function normalize(text: string): string {
  return text
    .trim()
    .replace(/^@\S+\s*/, '')
    .replace(/[\s。！!？?，,、.~～]+$/u, '')
    .trim();
}

/**
 * A message is a control command only when it is exactly one of the control
 * words; anything longer is a follow-up the worker should read.
 */
export function classifyIntent(text: string): Intent {
  const t = normalize(text);
  for (const [intent, words] of Object.entries(CONTROL_WORDS)) {
    if (words.includes(t)) return intent as Intent;
  }
  return 'followup';
}

/** "停 T0005" / "先别动t0012": a control word followed by a ticket id, and nothing else. */
export function explicitControl(text: string): { intent: Exclude<Intent, 'followup'>; ticket: string } | undefined {
  const m = /^(.+?)\s*[Tt](\d{4,})$/u.exec(normalize(text));
  if (!m) return undefined;
  const intent = classifyIntent(m[1]!);
  if (intent === 'followup') return undefined;
  return { intent, ticket: `T${m[2]}` };
}

/** Exit code and output of one `ticket` CLI call. */
export interface TicketResult {
  code: number;
  stdout: string;
  stderr: string;
}

/** Runs the `ticket` CLI; resolves undefined when it could not be run at all. */
export type TicketRunner = (args: string[]) => Promise<TicketResult | undefined>;

export interface FastPathMessage {
  messageId: string;
  content: string;
  /** The message this one reply-quotes, if any. */
  quotedId?: string;
  hasResources: boolean;
}

export interface FastPathOutcome {
  ticket: string;
  action: string;
  /** What to reply to the user: the CLI's own output, as the bridge session would relay it. */
  reply: string;
}

/**
 * Route a message that unambiguously belongs to a ticket straight to ticketd,
 * without starting the bridge session. Returns undefined to leave the message
 * to the bridge session (anything not clearly a ticket message).
 */
export async function routeTicketMessage(
  msg: FastPathMessage,
  run: TicketRunner,
): Promise<FastPathOutcome | undefined> {
  if (msg.hasResources) return undefined;
  const explicit = explicitControl(msg.content);
  if (explicit) {
    const done = await run([CONTROL_COMMAND[explicit.intent], explicit.ticket]);
    if (!done) return undefined;
    // Without a quote there is no context to read a refused 继续 / 执行 as an answer: let the session decide.
    if ((explicit.intent === 'continue' || explicit.intent === 'approve') && done.code !== 0) return undefined;
    return outcome(explicit.ticket, CONTROL_COMMAND[explicit.intent], done);
  }
  if (!msg.quotedId) return undefined;
  const found = await run(['lookup', '--message', msg.quotedId]);
  if (!found || found.code !== 0) return undefined;
  const ticket = found.stdout.trim();
  const intent = classifyIntent(msg.content);
  if (intent !== 'followup') {
    const cmd = CONTROL_COMMAND[intent];
    const done = await run([cmd, ticket]);
    if (!done) return undefined;
    // 继续 / 执行 only make sense in one state; otherwise they are plain answers to the worker.
    const fallsBack = (intent === 'continue' || intent === 'approve') && done.code !== 0;
    if (!fallsBack) return outcome(ticket, cmd, done);
  }
  const sent = await run(['send', ticket, '--message', msg.messageId, '--text', msg.content]);
  if (!sent) return undefined;
  return outcome(ticket, 'send', sent);
}

const CONTROL_COMMAND: Record<Exclude<Intent, 'followup'>, string> = {
  stop: 'stop',
  hold: 'hold',
  continue: 'release',
  approve: 'approve',
  restart: 'restart',
};

function outcome(ticket: string, action: string, r: TicketResult): FastPathOutcome {
  // A refused command explains itself on stderr ("错误：…"): relay that, as the bridge session would.
  const reply = (r.code === 0 ? r.stdout : r.stderr || r.stdout).trim();
  return { ticket, action, reply };
}

/**
 * Default runner: spawn the `ticket` CLI. Resolves undefined when it cannot be
 * started or does not finish within timeoutMs (the hung process is killed) —
 * the caller then leaves the message to the bridge session.
 */
export function createTicketRunner(bin: string, timeoutMs = 15_000): TicketRunner {
  return (args) =>
    new Promise((resolve) => {
      let stdout = '';
      let stderr = '';
      let settled = false;
      const done = (r: TicketResult | undefined) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(r);
      };
      const child = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
      const timer = setTimeout(() => {
        child.kill('SIGKILL');
        done(undefined);
      }, timeoutMs);
      child.stdout.setEncoding('utf8').on('data', (d: string) => (stdout += d));
      child.stderr.setEncoding('utf8').on('data', (d: string) => (stderr += d));
      child.on('error', () => done(undefined));
      child.on('close', (code) => done(code === null ? undefined : { code, stdout, stderr }));
    });
}

/** The message a reply quotes. In topic groups the topic root is structure, not a quote (same rule as the bridge). */
function quotedMessageId(msg: NormalizedMessage, chatMode: string): string | undefined {
  const replyTo = msg.replyToMessageId;
  if (!replyTo) return undefined;
  if (chatMode === 'topic' && msg.threadId && msg.rootId && replyTo === msg.rootId) return undefined;
  return replyTo;
}

/**
 * Larkyard: handle a message that unambiguously belongs to a ticket without
 * starting the bridge session. Enabled only when LARKYARD_TICKET_BIN names the
 * `ticket` CLI (Larkyard sets it on the bridge service it manages; unlike the
 * meeting-minutes button there is no PATH fallback, so an unmanaged bridge
 * behaves exactly like upstream). Returns false to let the session handle it.
 */
export async function tryTicketFastPath(input: {
  channel: LarkChannel;
  msg: NormalizedMessage;
  chatMode: string;
  bin?: string;
  run?: TicketRunner;
}): Promise<boolean> {
  const bin = input.bin ?? process.env.LARKYARD_TICKET_BIN;
  if (!bin) return false;
  const { msg } = input;
  const out = await routeTicketMessage(
    {
      messageId: msg.messageId,
      content: msg.content.replace(/^(@\S+\s*)+/u, '').trim(),
      quotedId: quotedMessageId(msg, input.chatMode),
      hasResources: msg.resources.length > 0,
    },
    input.run ?? createTicketRunner(bin),
  );
  if (!out) return false;
  log.info('intake', 'larkyard-fast-path', { msgId: msg.messageId, ticket: out.ticket, action: out.action });
  try {
    await input.channel.send(
      msg.chatId,
      { text: out.reply },
      { replyTo: msg.messageId, ...(input.chatMode === 'topic' && msg.threadId ? { replyInThread: true } : {}) },
    );
  } catch (err) {
    // The ticket command already ran: never fall back to the session, it would do it twice.
    log.warn('intake', 'larkyard-fast-path-reply-failed', { msgId: msg.messageId, err: String(err) });
  }
  return true;
}
