import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  handleMeetingTodo,
  meetingArchiveFile,
  meetingArchiveId,
  meetingMinutesCard,
  splitMinutesTodos,
  type MinutesRecord,
  type TicketNewRunner,
} from '../../../src/meeting/minutes';
import { meetingDataDir } from '../../../src/meeting/store';

describe('splitMinutesTodos', () => {
  it('takes todos from the last json block and strips it from the prose', () => {
    const answer = [
      '**讨论要点**\n- 发布\n```json\n{"not":"todos"}\n```',
      '```json',
      '{"todos":[{"task":"写发布说明","owner":"甲","due":"周五"},{"task":"回归测试","owner":"","due":""}]}',
      '```',
    ].join('\n');
    const r = splitMinutesTodos(answer);
    expect(r.parsed).toBe(true);
    expect(r.todos).toEqual([
      { task: '写发布说明', owner: '甲', due: '周五' },
      { task: '回归测试', owner: '', due: '' },
    ]);
    expect(r.minutes).toContain('**讨论要点**');
    expect(r.minutes).not.toContain('写发布说明');
  });

  it('keeps the prose verbatim without a block or with broken json', () => {
    expect(splitMinutesTodos('只有正文')).toEqual({ minutes: '只有正文', todos: [], parsed: false });
    const broken = '正文\n```json\n{todos: [}\n```';
    expect(splitMinutesTodos(broken)).toEqual({ minutes: broken, todos: [], parsed: false });
  });

  it('accepts a bare array, drops empty tasks and caps at 20', () => {
    const items = Array.from({ length: 25 }, (_, i) => ({ task: i === 3 ? ' ' : `任务${i}` }));
    const r = splitMinutesTodos('x\n```json\n' + JSON.stringify(items) + '\n```');
    expect(r.parsed).toBe(true);
    expect(r.todos).toHaveLength(20);
    expect(r.todos.some((t) => t.task === '任务3')).toBe(false);
  });
});

function record(over: Partial<MinutesRecord> = {}): MinutesRecord {
  return {
    id: '20261121-1430-123456789',
    meetingId: 'm1',
    meetingNo: '123456789',
    topic: '周会',
    endedAt: '2026-11-21T06:30:00.000Z',
    minutes: '纪要正文',
    todosParsed: true,
    todos: [
      { task: '写发布说明', owner: '甲', due: '周五' },
      { task: '回归测试', owner: '', due: '' },
    ],
    ...over,
  };
}

describe('meetingMinutesCard', () => {
  it('one button per todo carrying archive#n, unknown owner shown, archive id in footer', () => {
    const json = JSON.stringify(meetingMinutesCard(record(), ['本场没收到字幕']));
    expect(json).toContain('会议纪要 · 周会');
    expect(json.match(/派成工单"/g)).toHaveLength(2);
    expect(json).toContain('"arg":"20261121-1430-123456789#2"');
    expect(json).toContain('"cmd":"meeting.todo"');
    expect(json).toContain('负责人：未明确');
    expect(json).toContain('本场没收到字幕\\n存档 20261121-1430-123456789');
  });

  it('no buttons and no archive id when the archive failed', () => {
    const json = JSON.stringify(meetingMinutesCard(record(), [], false));
    expect(json).not.toContain('派成工单"');
    expect(json).not.toContain('存档 ');
  });

  it('says why there are no todos', () => {
    expect(JSON.stringify(meetingMinutesCard(record({ todos: [] }), []))).toContain('没有识别出明确的待办');
    expect(JSON.stringify(meetingMinutesCard(record({ todos: [], todosParsed: false }), []))).toContain(
      '待办没能结构化，以纪要正文为准',
    );
  });

  it('cuts long minutes and lists bound docs before the archive id', () => {
    const json = JSON.stringify(
      meetingMinutesCard(
        record({
          minutes: 'x'.repeat(9000),
          docs: [{ title: '方案', revision: 12, fetchedAt: '2026-11-21T01:00:00.000Z' }],
        }),
        [],
      ),
    );
    expect(json).toContain('全文见存档');
    expect(json).not.toContain('x'.repeat(8001));
    expect(json).toMatch(/会前资料：《方案》r12（[^）]+ 抓取）\\n存档 /);
  });
});

describe('meetingArchiveId', () => {
  it('is local YYYYMMDD-HHMM-<meeting no>', () => {
    expect(meetingArchiveId('123456789', new Date(2026, 10, 21, 9, 5).getTime())).toBe('20261121-0905-123456789');
  });
});

describe('handleMeetingTodo', () => {
  let root: string;
  let controls: { configPath: string; profile: string };
  let file: string;
  const ID = '20261121-1430-123456789';

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'meeting-todo-test-'));
    controls = { configPath: join(root, 'config.json'), profile: 'claude' };
    file = meetingArchiveFile(meetingDataDir(controls), ID);
    mkdirSync(join(meetingDataDir(controls), 'archive'), { recursive: true });
    writeFileSync(file, JSON.stringify(record()));
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  function setup(run: TicketNewRunner) {
    const replies: string[] = [];
    const sent: { to: string; input: unknown }[] = [];
    const channel = {
      send: vi.fn(async (to: string, input: unknown) => {
        sent.push({ to, input });
        return { messageId: 'om_note' };
      }),
    };
    const ctx = { controls, channel: channel as never, chatId: 'oc_1', senderId: 'ou_admin' };
    const go = (arg: string) => handleMeetingTodo(ctx, arg, async (md) => void replies.push(md), run);
    return { replies, sent, go };
  }

  it('posts a note, opens a ticket on it, records the ticket, relays the receipt', async () => {
    const run = vi.fn<TicketNewRunner>(async () => ({ code: 0, stdout: '🎫 已开单 T0061「写发布说明」\n', stderr: '' }));
    const { replies, sent, go } = setup(run);
    await go(`${ID}#1`);
    expect(sent).toHaveLength(1);
    expect(String((sent[0]!.input as { markdown: string }).markdown)).toContain('写发布说明');
    const input = run.mock.calls[0]![0];
    expect(input).toMatchObject({ chatId: 'oc_1', messageId: 'om_note', senderId: 'ou_admin' });
    expect(input.title.length).toBeLessThanOrEqual(15);
    expect(input.note.length).toBeLessThanOrEqual(30);
    expect(input.request).toContain('纪要正文');
    expect(JSON.parse(readFileSync(file, 'utf8')).todos[0].ticket).toBe('T0061');
    expect(replies).toEqual(['🎫 已开单 T0061「写发布说明」']);
  });

  it('does not dispatch the same todo twice', async () => {
    const run = vi.fn<TicketNewRunner>(async () => ({ code: 0, stdout: '🎫 已开单 T0061', stderr: '' }));
    const { replies, sent, go } = setup(run);
    await go(`${ID}#1`);
    await go(`${ID}#1`);
    expect(run).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(1);
    expect(replies[1]).toBe('这条待办已经派过单：T0061');
  });

  it('ignores a second click while the first is still running', async () => {
    let release!: () => void;
    const run = vi.fn<TicketNewRunner>(
      () => new Promise((r) => (release = () => r({ code: 0, stdout: '🎫 已开单 T0062', stderr: '' }))),
    );
    const { go } = setup(run);
    const first = go(`${ID}#2`);
    await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(1));
    await go(`${ID}#2`);
    release();
    await first;
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('reports a failure, records nothing, and a retry runs again', async () => {
    const run = vi
      .fn<TicketNewRunner>()
      .mockResolvedValueOnce({ code: 2, stdout: '', stderr: 'ticketd 没在运行' })
      .mockResolvedValueOnce({ code: 0, stdout: '🎫 已开单 T0063', stderr: '' });
    const { replies, go } = setup(run);
    await go(`${ID}#1`);
    expect(replies[0]).toBe('派单失败：ticketd 没在运行');
    expect(JSON.parse(readFileSync(file, 'utf8')).todos[0].ticket).toBeUndefined();
    await go(`${ID}#1`);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it('rejects malformed or traversing args without reading files, and unknown todos', async () => {
    const run = vi.fn<TicketNewRunner>();
    const { replies, sent, go } = setup(run);
    await go('abc');
    await go('../../config#1');
    await go(`${ID}#5`);
    expect(replies[0]).toContain('用法');
    expect(replies[1]).toContain('用法');
    expect(replies[2]).toContain('找不到这条待办');
    expect(run).not.toHaveBeenCalled();
    expect(sent).toHaveLength(0);
    expect(readdirSync(join(meetingDataDir(controls), 'archive'))).toEqual([`${ID}.json`]);
  });
});
