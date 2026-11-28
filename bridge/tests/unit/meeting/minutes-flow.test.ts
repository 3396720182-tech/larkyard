import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ startRunFlow: vi.fn() }));
vi.mock('../../../src/bot/run-flow', () => ({ startRunFlow: mocks.startRunFlow }));

const { summarizeEndedMeeting, answerInMeeting, buildMeetingPrompt } = await import(
  '../../../src/meeting/orchestrator'
);
const { MeetingSession } = await import('../../../src/meeting/session');
const { MEETING_DEFAULTS, createDefaultProfileConfig } = await import('../../../src/config/profile-schema');
const { meetingDataDir, writeMeetingJson } = await import('../../../src/meeting/store');
const { bindDoc, docSnapshotFile, meetingDocRefs } = await import('../../../src/meeting/docs');

import type { MeetingConfig } from '../../../src/config/profile-schema';

function fakeRun(text: string) {
  return {
    ok: true as const,
    execution: {
      subscribe: () => ({
        async *[Symbol.asyncIterator]() {
          yield { type: 'text', delta: text };
          yield { type: 'done' };
        },
      }),
    },
    policy: {},
    cwdRealpath: '/repo',
  };
}

const promptOf = (call = 0): string => String(mocks.startRunFlow.mock.calls[call]![0].prompt);

let root: string;
let controlsBase: { configPath: string; profile: string };

beforeEach(() => {
  vi.clearAllMocks();
  root = mkdtempSync(join(tmpdir(), 'meeting-flow-test-'));
  controlsBase = { configPath: join(root, 'config.json'), profile: 'claude' };
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

function setup(over: Partial<MeetingConfig> = {}, opts: { failCard?: boolean; failAll?: boolean } = {}) {
  const meeting: MeetingConfig = { ...MEETING_DEFAULTS, enabled: true, summaryOnEnd: true, ...over };
  const profileConfig = createDefaultProfileConfig({
    agentKind: 'claude',
    accounts: { app: { id: 'cli_test', secret: '${APP_SECRET}', tenant: 'feishu' } },
  });
  profileConfig.meeting = meeting;
  const session = new MeetingSession({
    client: { request: vi.fn(async () => ({ code: 0, data: {} }) as never) },
    meetingId: 'm1',
    meetingNo: '123456789',
    topic: '周会',
    config: meeting,
    originChatId: 'oc_team',
  });
  session.ingest({
    event_id: 'e1',
    activity_event_type: 'transcript_received',
    transcript_received_items: [{ sentence_id: 1, text: '甲说周五发布，乙负责写说明', speaker: { name: '甲' } }],
  });
  const sent: { to: string; input: { markdown?: string; card?: object } }[] = [];
  const args = {
    session,
    channel: {
      botIdentity: { name: 'bot' },
      send: vi.fn(async (to: string, input: { markdown?: string; card?: object }) => {
        if (opts.failAll || (opts.failCard && input.card)) throw new Error('rejected');
        sent.push({ to, input });
        return { messageId: 'om_1' };
      }),
    },
    controls: { ...controlsBase, profileConfig, botOwnerId: 'ou_owner' },
    executor: {},
    activeRuns: { interrupt: vi.fn() },
    sessions: {},
    workspaces: {},
  } as never;
  return { session, sent, args, dir: meetingDataDir(controlsBase) };
}

const MINUTES =
  '**讨论要点**\n- 发布\n**结论**\n- 周五发布\n**待办**\n1. 写说明\n```json\n{"todos":[{"task":"写发布说明","owner":"乙","due":"周五"}]}\n```';

describe('end-of-meeting minutes', () => {
  it('asks for three sections + json todos without the 200-character limit', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun(MINUTES));
    const { args } = setup();
    await summarizeEndedMeeting(args);
    const prompt = promptOf();
    expect(prompt).toContain('**讨论要点**、**结论**、**待办**');
    expect(prompt).toContain('未明确');
    expect(prompt).toContain('```json');
    expect(prompt).not.toContain('200 字内');
  });

  it('in-meeting answers keep the 200-character limit', () => {
    expect(buildMeetingPrompt({ question: 'q', transcript: [] })).toContain('200 字内');
  });

  it('archives minutes and todos (no transcript) and sends a card with a dispatch button', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun(MINUTES));
    const { args, sent, dir } = setup();
    await summarizeEndedMeeting(args);
    const files = readdirSync(join(dir, 'archive'));
    expect(files).toHaveLength(1);
    const raw = readFileSync(join(dir, 'archive', files[0]!), 'utf8');
    const rec = JSON.parse(raw);
    expect(rec).toMatchObject({ meetingId: 'm1', meetingNo: '123456789', todosParsed: true, docs: [] });
    expect(rec.todos).toEqual([{ task: '写发布说明', owner: '乙', due: '周五' }]);
    expect(raw).not.toContain('乙负责写说明'); // transcript text stays out
    expect(sent).toHaveLength(1);
    expect(sent[0]!.to).toBe('oc_team');
    const card = JSON.stringify(sent[0]!.input.card);
    expect(card).toContain(`"arg":"${rec.id}#1"`);
    expect(card).toContain(`存档 ${rec.id}`);
    expect(card).not.toContain('```json');
  });

  it('falls back to markdown when the card is rejected', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun(MINUTES));
    const { args, sent } = setup({}, { failCard: true });
    await summarizeEndedMeeting(args);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.input.markdown).toMatch(/^\*\*会议纪要 · 周会\*\*/);
  });

  it('when the archive cannot be written the card still goes out, without buttons', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun(MINUTES));
    const { args, sent, dir } = setup();
    // a file where the archive directory should be
    writeMeetingJson(join(dir, 'archive'), {});
    await summarizeEndedMeeting(args);
    const card = JSON.stringify(sent[0]!.input.card);
    expect(card).toContain('写发布说明');
    expect(card).not.toContain('派成工单"');
    expect(card).not.toContain('存档 ');
  });
});

describe('pre-meeting docs in answers', () => {
  const ref = {
    token: 'doxcnAAAAAAAAAAAAAAAA1',
    url: 'https://x.feishu.cn/docx/doxcnAAAAAAAAAAAAAAAA1', // check-private: ignore
    title: '方案',
    revision: 12,
    fetchedAt: '2026-11-21T01:00:00.000Z',
    chunks: 2,
    chars: 20,
    boundAt: new Date().toISOString(),
  };

  function bind(dir: string) {
    writeMeetingJson(docSnapshotFile(dir, ref.token, ref.revision), {
      ...ref,
      truncated: false,
      chunks: [
        { i: 1, text: '发布定在周五' },
        { i: 2, text: '预算十万' },
      ],
    });
    bindDoc(dir, { meetingId: 'm1', meetingNo: '123456789' }, ref);
  }

  it('puts cited chunks in the prompt, logs what was provided vs cited, cards the IM copy', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun('周五发布〔《方案》r12 §1〕，另见〔《方案》r12 §7〕'));
    const { args, sent, dir } = setup({ respondIn: 'im' });
    bind(dir);
    await answerInMeeting(args, '什么时候发布', { deliver: 'broadcast', askedBy: '丙' });
    const prompt = promptOf();
    expect(prompt).toContain('=== 会前资料');
    expect(prompt).toContain('〔《方案》r12 §1〕\n发布定在周五');
    expect(prompt).toContain('照抄方括号里的出处');
    const qa = JSON.parse(readFileSync(join(dir, 'qa', 'm1.jsonl'), 'utf8').trim());
    expect(qa.askedBy).toBe('丙');
    expect(qa.provided.map((s: { section: number }) => s.section)).toEqual([1, 2]);
    expect(qa.cited.map((s: { section: number }) => s.section)).toEqual([1]);
    expect(qa.unverified).toEqual([{ title: '方案', revision: 12, section: 7 }]);
    const card = JSON.stringify(sent[0]!.input.card);
    expect(card).toContain('依据：《方案》r12 §1（');
    expect(card).not.toContain('§2');
  });

  it('without docs the prompt has no doc section', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun('好'));
    const { args } = setup();
    await answerInMeeting(args, 'q', { deliver: 'caller' });
    expect(promptOf()).not.toContain('会前资料');
  });

  it('minutes list the docs, are not logged as Q&A, and the end releases the binding', async () => {
    mocks.startRunFlow.mockResolvedValue(fakeRun(MINUTES));
    const { args, sent, dir } = setup();
    bind(dir);
    await summarizeEndedMeeting(args);
    expect(promptOf()).toContain('本场会议绑定的会前资料：《方案》r12');
    expect(() => readFileSync(join(dir, 'qa', 'm1.jsonl'))).toThrow();
    expect(JSON.stringify(sent[0]!.input.card)).toContain('会前资料：《方案》r12');
    expect(meetingDocRefs(dir, 'm1')).toEqual([]);
  });

  it('the binding is released even when no summary is made', async () => {
    const { args, dir } = setup({ summaryOnEnd: false });
    bind(dir);
    await summarizeEndedMeeting(args);
    expect(meetingDocRefs(dir, 'm1')).toEqual([]);
  });
});
