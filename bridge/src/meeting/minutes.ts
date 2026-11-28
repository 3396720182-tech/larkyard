import { execFile } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { LarkChannel } from '@larksuite/channel';
import { log } from '../core/logger';
import { meetingDataDir, readMeetingJson, writeMeetingJson } from './store';

/**
 * Structured meeting minutes: the end-of-meeting prompt, splitting the answer
 * into prose + todos, the archive record, the minutes card, and turning one
 * todo into a Larkyard ticket (`/meeting todo <archive>#<n>`, admin-only via
 * the `/meeting` gate).
 *
 * The archive keeps the minutes and the todos only — never transcript or chat
 * text. Keeping a verbatim record is a data-retention decision of its own.
 */

/** Longer minutes are cut in the card; the archive keeps the full text. */
export const MINUTES_MAX_CHARS = 8000;
export const MAX_TODOS = 20;

export const MEETING_MINUTES_QUESTION = [
  '会议已结束。请基于以上会议记录（字幕和弹幕）写一份会议纪要，分三部分：**讨论要点**、**结论**、**待办**。',
  '待办写清做什么、负责人、截止时间；记录里没提到的写「未明确」，不要编。',
  '纪要正文写完后，另起一行输出一个 ```json 代码块，只放待办，格式：',
  '{"todos":[{"task":"做什么","owner":"负责人，没有就空字符串","due":"截止时间，没有就空字符串"}]}',
  '没有待办就输出 {"todos":[]}。代码块之后不要再写任何内容。',
].join('\n');

export interface MeetingTodo {
  task: string;
  owner: string;
  due: string;
  /** Ticket id once dispatched (`T0061`). */
  ticket?: string;
  ticketAt?: string;
}

/** A pre-meeting document as listed on the minutes (see `docs.ts`). */
export interface MinutesDocRef {
  title: string;
  revision: number | string;
  fetchedAt: string;
}

export interface MinutesRecord {
  id: string;
  meetingId: string;
  meetingNo: string;
  topic?: string;
  endedAt: string;
  originChatId?: string;
  minutes: string;
  todosParsed: boolean;
  todos: MeetingTodo[];
  /** Documents bound to this meeting (empty when none). */
  docs?: MinutesDocRef[];
}

/**
 * Split the minutes answer into prose and todos. The todos come from the
 * **last** ```json block (an earlier one may be quoted code). Without a usable
 * block the prose is kept verbatim and `parsed` is false.
 */
export function splitMinutesTodos(answer: string): {
  minutes: string;
  todos: MeetingTodo[];
  parsed: boolean;
} {
  const text = String(answer ?? '');
  let last: RegExpMatchArray | undefined;
  for (const m of text.matchAll(/```json\s*([\s\S]*?)```/gi)) last = m;
  const verbatim = { minutes: text.trim(), todos: [], parsed: false };
  if (!last || last.index === undefined) return verbatim;
  let arr: unknown;
  try {
    const j = JSON.parse(last[1] ?? '') as unknown;
    arr = Array.isArray(j) ? j : (j as { todos?: unknown } | null)?.todos;
  } catch {
    return verbatim;
  }
  if (!Array.isArray(arr)) return verbatim;
  const field = (t: unknown, k: string, max: number): string =>
    String((t as Record<string, unknown> | null)?.[k] ?? '')
      .trim()
      .slice(0, max);
  const todos = arr
    .map((t) => ({ task: field(t, 'task', 200), owner: field(t, 'owner', 40), due: field(t, 'due', 40) }))
    .filter((t) => t.task)
    .slice(0, MAX_TODOS);
  const minutes = (text.slice(0, last.index) + text.slice(last.index + last[0].length)).trim();
  return { minutes, todos, parsed: true };
}

/** `YYYYMMDD-HHMM-<meeting no>`, local time. */
export function meetingArchiveId(meetingNo: string, now = Date.now()): string {
  const d = new Date(now);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}-${meetingNo}`;
}

export function meetingArchiveFile(dataDir: string, id: string): string {
  return join(dataDir, 'archive', `${id}.json`);
}

/** `MM-DD HH:mm`, local time; `?` for an unparsable value. */
export function shortTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '?';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * The minutes card: prose, the todo list (one "派成工单" button each when the
 * archive was written), and a footer with the notes, bound documents and the
 * archive id.
 */
export function meetingMinutesCard(rec: MinutesRecord, notes: string[], archived = true): object {
  const title = `会议纪要 · ${rec.topic ?? rec.meetingNo}`;
  const minutes =
    rec.minutes.length > MINUTES_MAX_CHARS
      ? rec.minutes.slice(0, MINUTES_MAX_CHARS) + '\n…（太长已截断，全文见存档）'
      : rec.minutes;
  const elements: object[] = [{ tag: 'markdown', content: minutes }, { tag: 'hr' }];
  if (rec.todos.length) {
    elements.push({
      tag: 'markdown',
      content: `**待办 ${rec.todos.length} 条**${archived ? ' · 点「派成工单」交给 Larkyard 去做' : ''}`,
    });
    rec.todos.forEach((t, i) => {
      elements.push({
        tag: 'markdown',
        content: `${i + 1}. ${t.task}\n<font color='grey'>负责人：${t.owner || '未明确'} · 截止：${t.due || '未明确'}</font>`,
      });
      if (archived) {
        elements.push({
          tag: 'button',
          text: { tag: 'plain_text', content: '派成工单' },
          type: 'default',
          size: 'small',
          behaviors: [{ type: 'callback', value: { cmd: 'meeting.todo', arg: `${rec.id}#${i + 1}` } }],
        });
      }
    });
  } else {
    elements.push({
      tag: 'markdown',
      content: rec.todosParsed ? '没有识别出明确的待办。' : '待办没能结构化，以纪要正文为准。',
    });
  }
  const foot = [...notes];
  if (rec.docs?.length) {
    foot.push(
      '会前资料：' +
        rec.docs.map((r) => `《${r.title}》r${r.revision}（${shortTime(r.fetchedAt)} 抓取）`).join('、'),
    );
  }
  if (archived) foot.push(`存档 ${rec.id}`);
  if (foot.length) elements.push({ tag: 'markdown', content: foot.join('\n'), text_size: 'notation' });
  return {
    schema: '2.0',
    config: { summary: { content: title } },
    header: { title: { tag: 'plain_text', content: title }, template: 'green' },
    body: { elements },
  };
}

// ────────────── todo → Larkyard ticket ──────────────

export interface TicketNewInput {
  chatId: string;
  messageId: string;
  senderId: string;
  title: string;
  request: string;
  ack: string;
  note: string;
}

export interface TicketNewResult {
  code: number;
  stdout: string;
  stderr: string;
}

export type TicketNewRunner = (input: TicketNewInput) => Promise<TicketNewResult>;

/**
 * `ticket new` from Larkyard. The binary is `$LARKYARD_TICKET_BIN` (Larkyard
 * sets it on the bridge service it manages), else `ticket` on PATH. No shell;
 * the request goes through a temp file that is removed afterwards.
 */
export const runTicketNew: TicketNewRunner = async (input) => {
  const bin = process.env.LARKYARD_TICKET_BIN || 'ticket';
  const work = mkdtempSync(join(tmpdir(), 'meeting-todo-'));
  const requestFile = join(work, 'request.md');
  writeFileSync(requestFile, input.request);
  const args = [
    'new',
    '--chat', input.chatId,
    '--message', input.messageId,
    '--sender', input.senderId,
    '--title', input.title,
    '--slug', 'meeting-todo',
    '--request-file', requestFile,
    '--ack', input.ack,
    '--note', input.note,
  ];
  try {
    return await new Promise<TicketNewResult>((resolve) => {
      execFile(bin, args, { timeout: 60_000 }, (err, stdout, stderr) => {
        const code = err ? (typeof err.code === 'number' ? err.code : 1) : 0;
        const missing = (err as NodeJS.ErrnoException | null)?.code === 'ENOENT';
        resolve({
          code,
          stdout: String(stdout ?? ''),
          stderr: missing
            ? `找不到 ticket 命令（${bin}）。bridge 要由 Larkyard 管理，或设置 LARKYARD_TICKET_BIN。`
            : String(stderr ?? '') || (err && !stdout ? String(err.message) : ''),
        });
      });
    });
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
};

const inflight = new Set<string>();

export interface MeetingTodoContext {
  controls: { configPath: string; profile: string };
  channel: Pick<LarkChannel, 'send'>;
  chatId: string;
  senderId: string;
}

/**
 * `/meeting todo <archive>#<n>`: post a note describing the todo in the
 * current chat, open a ticket on that note (ticketd wants one message per
 * ticket), record the ticket id in the archive and relay ticketd's receipt.
 * A todo that already has a ticket is not dispatched again; a double click
 * while `ticket new` runs is ignored. Failures record nothing, so a retry works.
 */
export async function handleMeetingTodo(
  ctx: MeetingTodoContext,
  arg: string,
  reply: (markdown: string) => Promise<void>,
  run: TicketNewRunner = runTicketNew,
): Promise<void> {
  const m = /^(\d{8}-\d{4}-\d{9})#(\d{1,2})$/.exec(arg.trim());
  if (!m) {
    await reply('用法：在会议纪要卡片上点「派成工单」，或 `/meeting todo <存档号>#<第几条>`');
    return;
  }
  const archiveId = m[1]!;
  const index = Number(m[2]);
  const file = meetingArchiveFile(meetingDataDir(ctx.controls), archiveId);
  const rec = readMeetingJson<MinutesRecord | null>(file, null);
  const todo = rec?.todos?.[index - 1];
  if (!rec || !todo) {
    await reply(`找不到这条待办（存档 ${archiveId} 第 ${index} 条）。`);
    return;
  }
  if (todo.ticket) {
    await reply(`这条待办已经派过单：${todo.ticket}`);
    return;
  }
  const key = `${archiveId}#${index}`;
  if (inflight.has(key)) return;
  inflight.add(key);
  try {
    const topic = rec.topic ?? rec.meetingNo;
    const text = [
      `📌 会议待办转工单（来自「${topic}」的纪要）`,
      todo.task,
      `负责人：${todo.owner || '未明确'} · 截止：${todo.due || '未明确'}`,
    ].join('\n');
    const sent = await ctx.channel.send(ctx.chatId, { markdown: text });
    const out = await run({
      chatId: ctx.chatId,
      messageId: sent.messageId,
      senderId: ctx.senderId,
      title: todo.task.replace(/\s+/g, ' ').slice(0, 15),
      request: [text, '', '下面是这场会的纪要，供理解上下文：', '', rec.minutes].join('\n'),
      ack: `开工：会议待办「${todo.task.slice(0, 20)}」`,
      note: `会议纪要·${topic}`.slice(0, 30),
    });
    const ticketId = /\bT\d{4,}\b/.exec(out.stdout)?.[0];
    if (out.code === 0 && ticketId) {
      todo.ticket = ticketId;
      todo.ticketAt = new Date().toISOString();
      writeMeetingJson(file, rec);
    }
    log.info('meeting', 'todo-dispatched', { archive: archiveId, index, ticket: ticketId ?? null, code: out.code });
    await reply(
      out.code === 0
        ? out.stdout.trim() || `已派单 ${ticketId ?? ''}`
        : `派单失败：${(out.stderr || out.stdout).trim().slice(0, 300)}`,
    );
  } catch (err) {
    log.warn('meeting', 'todo-dispatch-failed', { archive: archiveId, err: String(err) });
    await reply(`派单失败：${String((err as Error)?.message ?? err)}`);
  } finally {
    inflight.delete(key);
  }
}
