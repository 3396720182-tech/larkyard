import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  adoptPendingDocs,
  bindDoc,
  chunkDoc,
  cleanDocText,
  docCitations,
  fetchDoc,
  handleMeetingDoc,
  loadDocBindings,
  meetingDocContext,
  meetingDocRefs,
  parseDocLinks,
  releaseMeetingDocs,
  retrieveDocChunks,
  type DocRef,
  type UsedChunk,
} from '../../../src/meeting/docs';
import { meetingDataDir } from '../../../src/meeting/store';
import type { VcRequestClient } from '../../../src/meeting/api';

const TOKEN = 'doxcnAAAAAAAAAAAAAAAA1';

function ref(over: Partial<DocRef> = {}): DocRef {
  return {
    token: TOKEN,
    url: `https://x.feishu.cn/docx/${TOKEN}`,
    title: '方案',
    revision: 12,
    fetchedAt: '2026-11-21T01:00:00.000Z',
    chunks: 2,
    chars: 20,
    boundAt: new Date().toISOString(),
    ...over,
  };
}

describe('parsing and chunking', () => {
  it('finds docx / wiki links, flags legacy docs, de-duplicates', () => {
    const links = parseDocLinks(
      `看 https://x.feishu.cn/docx/${TOKEN} 和 https://x.feishu.cn/wiki/wikcnBBBBBBBBBBBBBBBB2 ` + // check-private: ignore
        `还有 https://x.feishu.cn/docs/doccnCCCCCCCCCCCCCCCC3 以及 https://x.feishu.cn/docx/${TOKEN}`, // check-private: ignore
    );
    expect(links.map((l) => l.kind)).toEqual(['docx', 'wiki', 'doc']);
  });

  it('cleans zero-width characters and blank runs', () => {
    expect(cleanDocText('a\u200b\u00a0b  \r\n\n\n\nc')).toBe('a b\n\nc');
  });

  it('chunks stay within 500 characters and are numbered from 1', () => {
    const chunks = chunkDoc(['短段', '长'.repeat(1200), '尾段'].join('\n\n'));
    expect(chunks[0]!.i).toBe(1);
    expect(chunks.every((c) => c.text.length <= 500)).toBe(true);
    expect(chunks.map((c) => c.text).join('')).toContain('尾段');
  });

  it('gives everything for small docs, top matches for big ones', () => {
    const small = [{ ref: ref(), chunks: [{ i: 1, text: '排期周五' }, { i: 2, text: '预算十万' }] }];
    expect(retrieveDocChunks(small, '随便')).toHaveLength(2);
    const big = [
      {
        ref: ref(),
        chunks: Array.from({ length: 20 }, (_, i) => ({
          i: i + 1,
          text: (i === 7 ? '发布排期定在周五。' : '无关内容。') + '填'.repeat(400),
        })),
      },
    ];
    const got = retrieveDocChunks(big, '发布排期是哪天');
    expect(got[0]!.i).toBe(8);
    expect(got.length).toBeLessThanOrEqual(4);
  });
});

describe('docCitations', () => {
  it('cited = cited and provided; unverified = cited but never provided', () => {
    const provided: UsedChunk[] = [
      { doc: ref(), i: 1, text: 'a', score: 0 },
      { doc: ref(), i: 2, text: 'b', score: 0 },
    ];
    const r = docCitations('周五上线〔《方案》r12 §2、§9〕，预算见〔《方案》r11 §1〕', provided);
    expect(r.cited.map((c) => c.i)).toEqual([2]);
    expect(r.unverified).toEqual([
      { title: '方案', revision: 12, section: 9 },
      { title: '方案', revision: 11, section: 1 },
    ]);
  });
});

describe('fetchDoc', () => {
  function client(responses: Record<string, unknown>): VcRequestClient {
    return {
      request: vi.fn(async ({ url }: { url: string }) => {
        const key = Object.keys(responses).find((k) => url.endsWith(k));
        return (key ? responses[key] : { code: 404, msg: 'nope' }) as never;
      }),
    } as never;
  }

  it('resolves wiki to docx and snapshots title, revision and chunks', async () => {
    const c = client({
      '/get_node': { code: 0, data: { node: { obj_type: 'docx', obj_token: TOKEN } } },
      [`/documents/${TOKEN}`]: { code: 0, data: { document: { title: '《方案》', revision_id: 12 } } },
      '/raw_content': { code: 0, data: { content: '第一段\n\n第二段' } },
    });
    const doc = await fetchDoc(c, { kind: 'wiki', token: 'wikcnBBBBBBBBBBBBBBBB2', url: 'u' });
    expect(doc).toMatchObject({ token: TOKEN, title: '方案', revision: 12 });
    expect(doc.chunks).toEqual([{ i: 1, text: '第一段\n第二段' }]);
  });

  it('refuses legacy docs and non-docx wiki pages with a plain reason', async () => {
    await expect(fetchDoc(client({}), { kind: 'doc', token: 't', url: 'u' })).rejects.toThrow('旧版文档');
    const c = client({ '/get_node': { code: 0, data: { node: { obj_type: 'sheet' } } } });
    await expect(fetchDoc(c, { kind: 'wiki', token: 't', url: 'u' })).rejects.toThrow('sheet');
  });
});

describe('bindings', () => {
  let root: string;
  let controls: { configPath: string; profile: string };
  let dir: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'meeting-docs-test-'));
    controls = { configPath: join(root, 'config.json'), profile: 'claude' };
    dir = meetingDataDir(controls);
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  const session = (over: Record<string, unknown> = {}) =>
    ({ meetingId: 'm1', meetingNo: '111111111', originChatId: 'oc_1', ended: false, ...over }) as never;

  it('a waiting doc is taken only by the meeting it waits for', () => {
    bindDoc(dir, null, ref(), { meetingNo: '222222222' });
    bindDoc(dir, null, ref({ token: 'doxcnOTHERAAAAAAAAAAA9' }), { chatId: 'oc_1' });
    // auto-invited meeting: no origin chat, another number → takes nothing
    expect(adoptPendingDocs(dir, session({ originChatId: undefined }))).toHaveLength(0);
    expect(loadDocBindings(dir).pending).toHaveLength(2);
    // joined from oc_1 → takes the chat-scoped one only
    expect(adoptPendingDocs(dir, session()).map((r) => r.token)).toEqual(['doxcnOTHERAAAAAAAAAAA9']);
    const taken = adoptPendingDocs(dir, session({ meetingId: 'm2', meetingNo: '222222222', originChatId: 'oc_9' }));
    expect(taken.map((r) => r.token)).toEqual([TOKEN]);
    expect(meetingDocRefs(dir, 'm2')[0]).not.toHaveProperty('waitFor');
  });

  it('waiting docs expire after 12 hours', () => {
    bindDoc(dir, null, ref({ boundAt: new Date(Date.now() - 13 * 3600_000).toISOString() }), { chatId: 'oc_1' });
    expect(adoptPendingDocs(dir, session())).toHaveLength(0);
  });

  it('binding the same doc again replaces the revision; release drops the meeting', () => {
    bindDoc(dir, session(), ref());
    bindDoc(dir, session(), ref({ revision: 13 }));
    expect(meetingDocRefs(dir, 'm1').map((r) => r.revision)).toEqual([13]);
    releaseMeetingDocs(dir, 'm1');
    expect(meetingDocRefs(dir, 'm1')).toEqual([]);
  });

  it('a broken bindings file never blocks an answer', () => {
    bindDoc(dir, session(), ref());
    writeFileSync(join(dir, 'bindings.json'), '{oops');
    expect(meetingDocContext({ session: session(), controls } as never, 'q', [])).toEqual({
      refs: [],
      used: [],
      prompt: {},
    });
  });

  it('/meeting doc binds to the only live meeting, or waits for this chat', async () => {
    const c = {
      request: vi.fn(async ({ url }: { url: string }) =>
        url.endsWith('/raw_content')
          ? { code: 0, data: { content: '正文' } }
          : { code: 0, data: { document: { title: '方案', revision_id: 12 } } },
      ),
    };
    const replies: string[] = [];
    const ctx = (sessions: never[]) => ({
      controls,
      channel: { rawClient: c } as never,
      chatId: 'oc_1',
      senderId: 'ou_admin',
      sessions: () => sessions,
    });
    const link = `https://x.feishu.cn/docx/${TOKEN}`;
    await handleMeetingDoc(ctx([session()]), link, async (m) => void replies.push(m));
    expect(meetingDocRefs(dir, 'm1')).toHaveLength(1);
    expect(replies[0]).toContain('已绑定到会议');
    await handleMeetingDoc(ctx([]), link, async (m) => void replies.push(m));
    expect(loadDocBindings(dir).pending[0]!.waitFor).toEqual({ chatId: 'oc_1' });
    expect(JSON.parse(readFileSync(join(dir, 'docs', `${TOKEN}@r12.json`), 'utf8')).chunks).toHaveLength(1);
  });
});
