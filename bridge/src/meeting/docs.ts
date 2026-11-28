import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { LarkChannel } from '@larksuite/channel';
import { log } from '../core/logger';
import { call, extractMeetingNo, type VcRequestClient } from './api';
import { shortTime } from './minutes';
import type { MeetingSession } from './session';
import { meetingDataDir, readMeetingJson, writeMeetingJson } from './store';
import type { ShareEvent } from './types';

/**
 * Pre-meeting documents: an admin binds a Feishu doc to a meeting, the bridge
 * snapshots and chunks it, and in-meeting answers retrieve chunks and cite
 * them as 〔《title》r<revision> §n〕.
 *
 * - Documents are read with the **bot's** identity only. A doc the bot cannot
 *   read cannot be bound; the owner's or asker's identity is never borrowed.
 * - Not filtered by the asker's permissions: binding is the admin's consent
 *   that this doc may be quoted in this meeting, where every answer is visible
 *   to all participants anyway.
 * - Snapshots are immutable per revision; bindings refer to (token, revision),
 *   so an old answer always maps back to the text it was given.
 * - Every in-meeting answer is logged with what was provided and what was
 *   actually cited (and citations that match nothing provided).
 *
 * Data: `<profileDir>/meeting-data/` — `docs/<token>@r<rev>.json`,
 * `bindings.json`, `qa/<meetingId>.jsonl`.
 */

export const DOC_MAX_CHARS = 200_000;
export const DOC_CHUNK = 500;
export const DOC_TOP_K = 4;
/** Total size under which every chunk is given (missing one costs more than sending all). */
export const DOC_BUDGET = 6000;
export const DOC_PENDING_TTL_MS = 12 * 3600 * 1000;
const DOC_KEEP_MS = 7 * 24 * 3600 * 1000;
export const DOC_MAX_LINKS = 5;

export interface DocLink {
  kind: 'docx' | 'wiki' | 'doc';
  token: string;
  url: string;
}

export interface DocChunk {
  i: number;
  text: string;
}

export interface DocSnapshot {
  token: string;
  url: string;
  title: string;
  revision: number;
  fetchedAt: string;
  chars: number;
  truncated: boolean;
  chunks: DocChunk[];
}

export interface DocRef {
  token: string;
  url: string;
  title: string;
  revision: number;
  fetchedAt: string;
  /** Number of chunks. */
  chunks: number;
  chars: number;
  boundAt: string;
  truncated?: boolean;
  /** Binder's open_id, or `share` when picked up from an in-meeting share. */
  by?: string;
  /** Chat the binding was made in (notified when a waiting doc is picked up). */
  boundIn?: string;
}

/** Which meeting may pick up a waiting doc. */
export type DocWaitFor = { meetingNo: string } | { chatId: string };

export type PendingDoc = DocRef & { waitFor?: Partial<{ meetingNo: string; chatId: string }> };

export interface DocBindings {
  pending: PendingDoc[];
  meetings: Record<string, { meetingNo: string; topic?: string; docs: DocRef[]; updatedAt?: string }>;
}

/** A chunk selected for a prompt. */
export interface UsedChunk {
  doc: DocRef;
  i: number;
  text: string;
  score: number;
}

/** Meeting the doc binds to right away. */
export interface DocTarget {
  meetingId: string;
  meetingNo: string;
  topic?: string;
}

// ────────────── parsing, cleaning, chunking, retrieval ──────────────

/** Feishu doc links in text: new docs (`/docx/`), wiki pages (`/wiki/`); legacy `/doc/` is recognized but unsupported. */
export function parseDocLinks(text: string): DocLink[] {
  const out: DocLink[] = [];
  const re = /https?:\/\/[^\s<>"'()（）[\]]+?\/(docx|wiki|docs|doc)\/([A-Za-z0-9]{16,})/g;
  for (const m of String(text ?? '').matchAll(re)) {
    const token = m[2]!;
    if (out.some((o) => o.token === token)) continue;
    const kind = m[1] === 'docx' || m[1] === 'wiki' ? m[1] : 'doc';
    out.push({ kind, token, url: m[0] });
  }
  return out;
}

/** Normalize newlines, drop zero-width characters and trailing blanks, squeeze blank lines, cap the size. */
export function cleanDocText(raw: string): string {
  return String(raw ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/[\u200b-\u200d\u2060\ufeff]/g, '')
    .replace(/[ \t]+$/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
    .slice(0, DOC_MAX_CHARS);
}

/**
 * Split on blank lines, cut long paragraphs at sentence ends, merge short
 * ones; no chunk exceeds {@link DOC_CHUNK}. Numbered from 1, stable within a
 * revision.
 */
export function chunkDoc(text: string): DocChunk[] {
  const pieces: string[] = [];
  for (const para of text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)) {
    if (para.length <= DOC_CHUNK) {
      pieces.push(para);
      continue;
    }
    let buf = '';
    for (const s of para.split(/(?<=[。！？!?；;\n])/)) {
      if (buf && buf.length + s.length > DOC_CHUNK) {
        pieces.push(buf.trim());
        buf = '';
      }
      buf += s;
      while (buf.length > DOC_CHUNK) {
        pieces.push(buf.slice(0, DOC_CHUNK).trim());
        buf = buf.slice(DOC_CHUNK);
      }
    }
    if (buf.trim()) pieces.push(buf.trim());
  }
  const chunks: string[] = [];
  let cur = '';
  for (const p of pieces) {
    if (cur && cur.length + p.length + 1 > DOC_CHUNK) {
      chunks.push(cur);
      cur = '';
    }
    cur = cur ? `${cur}\n${p}` : p;
  }
  if (cur) chunks.push(cur);
  return chunks.map((t, i) => ({ i: i + 1, text: t }));
}

/** Search terms without a tokenizer: latin words / numbers whole, CJK as bigrams. */
export function docTerms(text: string): Set<string> {
  const t = String(text ?? '').toLowerCase();
  const terms = new Set<string>();
  for (const m of t.matchAll(/[a-z0-9][a-z0-9._-]*/g)) if (m[0].length >= 2) terms.add(m[0]);
  for (const run of t.match(/[\u3400-\u9fff]+/g) ?? []) {
    for (let i = 0; i + 1 < run.length; i++) terms.add(run.slice(i, i + 2));
  }
  return terms;
}

/**
 * Pick chunks for a question. Everything when the total fits the budget;
 * otherwise idf-weighted term overlap, top K (K × chunk size ≤ budget).
 */
export function retrieveDocChunks(
  docs: { ref: DocRef; chunks: DocChunk[] }[],
  query: string,
  opts: { budget?: number; topK?: number } = {},
): UsedChunk[] {
  const budget = opts.budget ?? DOC_BUDGET;
  const topK = opts.topK ?? DOC_TOP_K;
  const all: Omit<UsedChunk, 'score'>[] = [];
  for (const d of docs) for (const c of d.chunks) all.push({ doc: d.ref, i: c.i, text: c.text });
  if (all.length === 0) return [];
  const total = all.reduce((n, x) => n + x.text.length, 0);
  if (total <= budget) return all.map((x) => ({ ...x, score: 0 }));
  const q = [...docTerms(query)];
  if (q.length === 0) return [];
  const termsOf = all.map((x) => docTerms(x.text));
  const df = new Map<string, number>();
  for (const ts of termsOf) for (const t of q) if (ts.has(t)) df.set(t, (df.get(t) ?? 0) + 1);
  const scored: UsedChunk[] = [];
  all.forEach((x, k) => {
    let s = 0;
    for (const t of q) if (termsOf[k]!.has(t)) s += Math.log(1 + all.length / df.get(t)!);
    if (s > 0) scored.push({ ...x, score: s });
  });
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, topK);
}

export function docLabel(ref: Pick<DocRef, 'title' | 'revision'>): string {
  return `《${ref.title}》r${ref.revision}`;
}

/** `依据：《A》r12 §3、§5（…抓取）；《B》r4 §1`, or '' when nothing was used. */
export function docSourcesText(used: Pick<UsedChunk, 'doc' | 'i'>[]): string {
  if (!used.length) return '';
  const byDoc = new Map<string, { ref: DocRef; secs: number[] }>();
  for (const u of used) {
    const key = `${u.doc.token}@${u.doc.revision}`;
    if (!byDoc.has(key)) byDoc.set(key, { ref: u.doc, secs: [] });
    byDoc.get(key)!.secs.push(u.i);
  }
  return (
    '依据：' +
    [...byDoc.values()]
      .map(
        ({ ref, secs }) =>
          `${docLabel(ref)} ${secs.sort((a, b) => a - b).map((i) => `§${i}`).join('、')}（${shortTime(ref.fetchedAt)} 抓取）`,
      )
      .join('；')
  );
}

/**
 * Citations in an answer: `cited` are those that match a provided chunk (the
 * real grounds), `unverified` are cited but never provided (made up, or
 * another revision).
 */
export function docCitations(
  answer: string,
  provided: UsedChunk[],
): { cited: UsedChunk[]; unverified: { title: string; revision: number; section: number }[] } {
  const cited: UsedChunk[] = [];
  const unverified: { title: string; revision: number; section: number }[] = [];
  const re = /《([^》]+)》\s*r(\d+)\s*((?:§\s*\d+[\s、,，和及]*)+)/g;
  for (const m of String(answer ?? '').matchAll(re)) {
    const title = m[1]!;
    const revision = Number(m[2]);
    for (const n of m[3]!.matchAll(/§\s*(\d+)/g)) {
      const section = Number(n[1]);
      const hit = provided.find(
        (u) => u.doc.title === title && Number(u.doc.revision) === revision && u.i === section,
      );
      if (hit) {
        if (!cited.includes(hit)) cited.push(hit);
      } else if (!unverified.some((x) => x.title === title && x.revision === revision && x.section === section)) {
        unverified.push({ title, revision, section });
      }
    }
  }
  return { cited, unverified };
}

// ────────────── bindings ──────────────

const bindingsFile = (dir: string) => join(dir, 'bindings.json');
export const docSnapshotFile = (dir: string, token: string, revision: number | string) =>
  join(dir, 'docs', `${token}@r${revision}.json`);

export function loadDocBindings(dir: string, strict = false): DocBindings {
  const b = readMeetingJson<Partial<DocBindings> | null>(bindingsFile(dir), {}, strict);
  return {
    pending: Array.isArray(b?.pending) ? b.pending : [],
    meetings: b?.meetings && typeof b.meetings === 'object' ? b.meetings : {},
  };
}

function age(now: number, iso: string | undefined): number {
  return now - (Date.parse(iso ?? '') || 0);
}

export function saveDocBindings(dir: string, b: DocBindings, now = Date.now()): void {
  b.pending = b.pending.filter((r) => age(now, r.boundAt) < DOC_PENDING_TTL_MS);
  for (const [id, m] of Object.entries(b.meetings)) {
    if (age(now, m.updatedAt) > DOC_KEEP_MS) delete b.meetings[id];
  }
  writeMeetingJson(bindingsFile(dir), b);
}

/** Binding the same doc again replaces it with the new revision (old snapshot kept). */
function upsertRef(list: DocRef[], ref: DocRef): void {
  const i = list.findIndex((r) => r.token === ref.token);
  if (i >= 0) list[i] = ref;
  else list.push(ref);
}

export function waitText(waitFor: PendingDoc['waitFor']): string {
  return waitFor?.meetingNo ? `等会议 ${waitFor.meetingNo}` : '等在绑定所在的会话里让我入会的下一场';
}

function sameWait(a: PendingDoc['waitFor'], b: PendingDoc['waitFor']): boolean {
  return (a?.meetingNo ?? null) === (b?.meetingNo ?? null) && (a?.chatId ?? null) === (b?.chatId ?? null);
}

/** Bind to a live meeting, or queue until the meeting `waitFor` names. */
export function bindDoc(dir: string, target: DocTarget | null, ref: DocRef, waitFor?: DocWaitFor): void {
  const b = loadDocBindings(dir);
  if (!target) {
    const i = b.pending.findIndex((r) => r.token === ref.token && sameWait(r.waitFor, waitFor));
    const entry: PendingDoc = { ...ref, ...(waitFor ? { waitFor } : {}) };
    if (i >= 0) b.pending[i] = entry;
    else b.pending.push(entry);
  } else {
    const m = b.meetings[target.meetingId] ?? {
      meetingNo: target.meetingNo,
      ...(target.topic ? { topic: target.topic } : {}),
      docs: [],
    };
    upsertRef(m.docs, ref);
    m.updatedAt = new Date().toISOString();
    b.meetings[target.meetingId] = m;
  }
  saveDocBindings(dir, b);
}

/**
 * On join, take the waiting docs meant for this meeting: same meeting number,
 * or same origin chat. Others stay (only expired ones go) — a meeting the bot
 * was auto-invited to must not walk off with docs prepared for another one.
 */
export function adoptPendingDocs(
  dir: string,
  session: Pick<MeetingSession, 'meetingId' | 'meetingNo' | 'topic' | 'originChatId'>,
  now = Date.now(),
): PendingDoc[] {
  const b = loadDocBindings(dir);
  if (b.pending.length === 0) return [];
  const fresh = b.pending.filter((r) => age(now, r.boundAt) < DOC_PENDING_TTL_MS);
  const mine = (r: PendingDoc) =>
    r.waitFor?.meetingNo
      ? r.waitFor.meetingNo === session.meetingNo
      : Boolean(r.waitFor?.chatId) && r.waitFor?.chatId === session.originChatId;
  const adopted = fresh.filter(mine);
  b.pending = fresh.filter((r) => !mine(r));
  if (adopted.length > 0) {
    const m = b.meetings[session.meetingId] ?? {
      meetingNo: session.meetingNo,
      ...(session.topic ? { topic: session.topic } : {}),
      docs: [],
    };
    for (const { waitFor: _w, ...r } of adopted) upsertRef(m.docs, r);
    m.updatedAt = new Date(now).toISOString();
    b.meetings[session.meetingId] = m;
  }
  saveDocBindings(dir, b, now);
  return adopted;
}

export function meetingDocRefs(dir: string, meetingId: string, strict = false): DocRef[] {
  return loadDocBindings(dir, strict).meetings[meetingId]?.docs ?? [];
}

export function loadDocSnapshots(
  dir: string,
  refs: DocRef[],
  strict = false,
): { ref: DocRef; chunks: DocChunk[] }[] {
  const out: { ref: DocRef; chunks: DocChunk[] }[] = [];
  for (const ref of refs) {
    const d = readMeetingJson<DocSnapshot | null>(docSnapshotFile(dir, ref.token, ref.revision), null, strict);
    if (d && Array.isArray(d.chunks)) out.push({ ref, chunks: d.chunks });
  }
  return out;
}

/** Drop a meeting's bindings once it has ended. Snapshots stay. */
export function releaseMeetingDocs(dir: string, meetingId: string): void {
  const b = loadDocBindings(dir);
  const m = b.meetings[meetingId];
  if (!m) return;
  delete b.meetings[meetingId];
  saveDocBindings(dir, b);
  log.info('meeting', 'docs-released', { meetingId, docs: m.docs?.length ?? 0 });
}

// ────────────── fetching ──────────────

/** A user-facing reason the doc cannot be used. */
export class MeetingDocError extends Error {}

export async function fetchDoc(client: VcRequestClient, link: DocLink): Promise<DocSnapshot> {
  let token = link.token;
  if (link.kind === 'wiki') {
    const data = await call<{ node?: { obj_type?: string; obj_token?: string } }>(client, {
      method: 'GET',
      url: '/open-apis/wiki/v2/spaces/get_node',
      params: { token },
    });
    const node = data.node ?? {};
    if (node.obj_type !== 'docx' || !node.obj_token) {
      throw new MeetingDocError(`这个知识库页面是 ${node.obj_type ?? '未知'} 类型，目前只支持新版文档（docx）`);
    }
    token = node.obj_token;
  } else if (link.kind !== 'docx') {
    throw new MeetingDocError('旧版文档（/doc/、/docs/）不支持，请先转成新版文档');
  }
  const meta = await call<{ document?: { title?: string; revision_id?: number } }>(client, {
    method: 'GET',
    url: `/open-apis/docx/v1/documents/${token}`,
  });
  const raw = await call<{ content?: string }>(client, {
    method: 'GET',
    url: `/open-apis/docx/v1/documents/${token}/raw_content`,
  });
  const doc = meta.document ?? {};
  const text = cleanDocText(raw.content ?? '');
  if (!text) throw new MeetingDocError('文档是空的（或者只有图片、表格这类读不出文字的内容）');
  return {
    token,
    url: link.url,
    title: String(doc.title || '未命名文档').replace(/[《》]/g, ''),
    revision: doc.revision_id ?? 0,
    fetchedAt: new Date().toISOString(),
    chars: text.length,
    truncated: String(raw.content ?? '').length > DOC_MAX_CHARS,
    chunks: chunkDoc(text),
  };
}

export function docErrorText(err: unknown): string {
  if (err instanceof MeetingDocError) return err.message;
  const e = err as { code?: unknown; message?: string } | undefined;
  if (typeof e?.code === 'number') {
    return `bot 读不到这份文档（错误码 ${e.code}：${e.message}）。链接没问题的话，在文档右上角「分享」里把机器人加为可阅读，再发一次。`;
  }
  return String(e?.message ?? err);
}

export async function ingestDoc(opts: {
  client: VcRequestClient;
  dir: string;
  link: DocLink;
  target: DocTarget | null;
  waitFor?: DocWaitFor;
  by?: string;
  boundIn?: string;
}): Promise<DocRef> {
  const doc = await fetchDoc(opts.client, opts.link);
  writeMeetingJson(docSnapshotFile(opts.dir, doc.token, doc.revision), doc);
  const ref: DocRef = {
    token: doc.token,
    url: doc.url,
    title: doc.title,
    revision: doc.revision,
    fetchedAt: doc.fetchedAt,
    chunks: doc.chunks.length,
    chars: doc.chars,
    boundAt: doc.fetchedAt,
    ...(doc.truncated ? { truncated: true } : {}),
    ...(opts.by ? { by: opts.by } : {}),
    ...(opts.boundIn ? { boundIn: opts.boundIn } : {}),
  };
  bindDoc(opts.dir, opts.target, ref, opts.waitFor);
  log.info('meeting', 'doc-bound', {
    token: doc.token.slice(-6),
    revision: doc.revision,
    chunks: doc.chunks.length,
    to: opts.target ? opts.target.meetingNo : waitText(opts.waitFor),
  });
  return ref;
}

// ────────────── in meeting ──────────────

interface DocDeps {
  session: MeetingSession;
  channel: LarkChannel;
  controls: { configPath: string; profile: string; botOwnerId?: string };
}

export interface DocContext {
  refs: DocRef[];
  used: UsedChunk[];
  prompt: { materials?: UsedChunk[]; materialList?: DocRef[] };
}

/** Chunks for this question (minutes only get the list). Never throws: docs must not block an answer. */
export function meetingDocContext(
  deps: Pick<DocDeps, 'session' | 'controls'>,
  question: string,
  transcript: string[],
  purpose?: 'minutes',
): DocContext {
  try {
    const dir = meetingDataDir(deps.controls);
    const refs = meetingDocRefs(dir, deps.session.meetingId, true);
    if (refs.length === 0) return { refs, used: [], prompt: {} };
    if (purpose === 'minutes') return { refs, used: [], prompt: { materialList: refs } };
    const query = [question, ...transcript.slice(-2)].join('\n');
    const used = retrieveDocChunks(loadDocSnapshots(dir, refs, true), query);
    return { refs, used, prompt: { materials: used, materialList: refs } };
  } catch (err) {
    log.warn('meeting', 'doc-context-failed', { meetingId: deps.session.meetingId, err: String(err) });
    return { refs: [], used: [], prompt: {} };
  }
}

const qaSections = (list: UsedChunk[]) =>
  list.map((u) => ({ token: u.doc.token, revision: u.doc.revision, title: u.doc.title, section: u.i }));

/** One line per in-meeting answer: who asked what, what was provided, what was cited. */
export function recordMeetingQa(
  deps: Pick<DocDeps, 'session' | 'controls'>,
  entry: {
    question: string;
    answer: string;
    askedBy?: string;
    provided: UsedChunk[];
    cited: UsedChunk[];
    unverified: { title: string; revision: number; section: number }[];
  },
): void {
  try {
    const file = join(meetingDataDir(deps.controls), 'qa', `${deps.session.meetingId}.jsonl`);
    mkdirSync(dirname(file), { recursive: true });
    const cited = qaSections(entry.cited);
    appendFileSync(
      file,
      JSON.stringify({
        at: new Date().toISOString(),
        meetingNo: deps.session.meetingNo,
        ...(entry.askedBy ? { askedBy: entry.askedBy } : {}),
        question: entry.question,
        answer: entry.answer,
        provided: qaSections(entry.provided),
        cited,
        unverified: entry.unverified,
      }) + '\n',
    );
    if (cited.length || entry.unverified.length) {
      log.info('meeting', 'docs-used', {
        meetingId: deps.session.meetingId,
        cited: cited.map((s) => `${s.token.slice(-6)}@r${s.revision}§${s.section}`).join(','),
        unverified: entry.unverified.length,
      });
    }
  } catch (err) {
    log.warn('meeting', 'qa-record-failed', { err: String(err) });
  }
}

export function meetingAnswerCard(
  session: Pick<MeetingSession, 'topic' | 'meetingNo'>,
  question: string,
  answer: string,
  askedBy: string | undefined,
  cited: UsedChunk[],
): object {
  const elements: object[] = [
    { tag: 'markdown', content: `**${askedBy ?? '会中提问'}：**${question}` },
    { tag: 'markdown', content: answer },
  ];
  const src = docSourcesText(cited);
  if (src) elements.push({ tag: 'hr' }, { tag: 'markdown', content: src, text_size: 'notation' });
  const title = `会中回答 · ${session.topic ?? session.meetingNo}`;
  return {
    schema: '2.0',
    config: { summary: { content: title } },
    header: { title: { tag: 'plain_text', content: title }, template: 'blue' },
    body: { elements },
  };
}

/** An IM copy of an in-meeting answer as a card whose sources are the cited chunks; markdown when the card fails. */
export async function sendAnswerToIm(
  deps: Pick<DocDeps, 'session' | 'channel'>,
  to: string,
  question: string,
  answer: string,
  askedBy: string | undefined,
  cited: UsedChunk[],
): Promise<void> {
  try {
    await deps.channel.send(to, { card: meetingAnswerCard(deps.session, question, answer, askedBy, cited) });
  } catch (err) {
    log.warn('meeting', 'answer-card-failed', { err: String(err) });
    const src = docSourcesText(cited);
    await deps.channel.send(to, { markdown: src ? `${answer}\n\n> ${src}` : answer });
  }
}

/** A Feishu doc shared (magic share) in the meeting becomes a doc of this meeting. Failures are only logged. */
export function attachShareDocs(deps: DocDeps): void {
  const { session } = deps;
  session.on('share', (event) => {
    const share = event as ShareEvent;
    if (share.action !== 'started' || !share.url) return;
    const link = parseDocLinks(share.url)[0];
    if (!link) return;
    let dir: string;
    try {
      dir = meetingDataDir(deps.controls);
      if (meetingDocRefs(dir, session.meetingId).some((r) => r.url === link.url)) return;
    } catch (err) {
      log.warn('meeting', 'share-doc-failed', { meetingId: session.meetingId, err: String(err) });
      return;
    }
    void ingestDoc({
      client: deps.channel.rawClient as unknown as VcRequestClient,
      dir,
      link,
      target: session,
      by: 'share',
    }).catch((err) =>
      log.warn('meeting', 'share-doc-failed', { meetingId: session.meetingId, err: docErrorText(err) }),
    );
  });
}

/** Take waiting docs on join and tell the chat each was bound in. */
export function adoptPendingDocsOnJoin(deps: DocDeps): void {
  const { session } = deps;
  try {
    const adopted = adoptPendingDocs(meetingDataDir(deps.controls), session);
    if (adopted.length === 0) return;
    log.info('meeting', 'docs-adopted', { meetingId: session.meetingId, count: adopted.length });
    // The binder's chat: an auto-invited meeting has no origin chat.
    const byChat = new Map<string, PendingDoc[]>();
    for (const r of adopted) {
      const to = r.boundIn ?? session.originChatId ?? deps.controls.botOwnerId;
      if (!to) continue;
      if (!byChat.has(to)) byChat.set(to, []);
      byChat.get(to)!.push(r);
    }
    for (const [to, refs] of byChat) {
      void deps.channel
        .send(to, {
          markdown: `📎 已把会前资料带进会议「${session.topic ?? session.meetingNo}」（${session.meetingNo}）：${refs.map(docLabel).join('、')}`,
        })
        .catch((err) => log.warn('meeting', 'docs-adopted-notice-failed', { err: String(err) }));
    }
  } catch (err) {
    log.warn('meeting', 'docs-adopt-failed', { err: String(err) });
  }
}

// ────────────── commands: /meeting doc, /meeting docs ──────────────

export interface MeetingDocCommandContext {
  controls: { configPath: string; profile: string };
  channel: LarkChannel;
  chatId: string;
  senderId: string;
  /** Meetings currently followed. */
  sessions: () => MeetingSession[];
}

const DOC_USAGE =
  '用法：`/meeting doc <飞书文档链接> [会议号]`，一次最多 5 个链接。也可以直接发「会前资料 <链接> [会议号]」。\n' +
  '带会议号：绑到那场会（还没开就等它开）。不带：有一场进行中的会就绑到它；没有会时，等你在这个会话里让我入会的下一场。12 小时内有效。';

/** `/meeting doc <links> [meeting no]` and `/meeting doc clear [meeting no]`. */
export async function handleMeetingDoc(
  ctx: MeetingDocCommandContext,
  rest: string,
  reply: (markdown: string) => Promise<void>,
): Promise<void> {
  const dir = meetingDataDir(ctx.controls);
  const live = ctx.sessions().filter((s) => !s.ended);
  const links = parseDocLinks(rest);
  const restNoLinks = rest.replace(/https?:\/\/\S+/g, ' ');
  if (/^(clear|清空)(\s|$)/.test(rest.trim())) {
    const picked = extractMeetingNo(restNoLinks.trim().replace(/^(clear|清空)/, ''));
    const b = loadDocBindings(dir);
    const targets = picked.ok ? live.filter((s) => s.meetingNo === picked.meetingNo) : live;
    for (const s of targets) delete b.meetings[s.meetingId];
    const before = b.pending.length;
    b.pending = picked.ok ? b.pending.filter((r) => r.waitFor?.meetingNo !== picked.meetingNo) : [];
    const dropped = before - b.pending.length;
    saveDocBindings(dir, b);
    await reply(
      `✅ 已清空 ${targets.length} 场进行中会议的资料${dropped ? `，以及 ${dropped} 份等待中的资料` : ''}。快照文件保留，旧回答仍能对上原文。`,
    );
    return;
  }
  if (links.length === 0) {
    await reply(DOC_USAGE);
    return;
  }
  let target: MeetingSession | null = null;
  let waitFor: DocWaitFor | undefined;
  const picked = extractMeetingNo(restNoLinks);
  if (picked.ok) {
    target = live.find((s) => s.meetingNo === picked.meetingNo) ?? null;
    if (!target) waitFor = { meetingNo: picked.meetingNo };
  } else if (live.length === 1) {
    target = live[0]!;
  } else if (live.length > 1) {
    await reply(
      '现在有多场会议在进行，请带上会议号：`/meeting doc <链接> <会议号>`\n' +
        live.map((s) => `- ${s.topic ?? s.meetingNo}（${s.meetingNo}）`).join('\n'),
    );
    return;
  } else {
    waitFor = { chatId: ctx.chatId };
  }
  const lines: string[] = [];
  for (const link of links.slice(0, DOC_MAX_LINKS)) {
    try {
      const ref = await ingestDoc({
        client: ctx.channel.rawClient as unknown as VcRequestClient,
        dir,
        link,
        target,
        ...(waitFor ? { waitFor } : {}),
        by: ctx.senderId,
        boundIn: ctx.chatId,
      });
      lines.push(
        `✅ ${docLabel(ref)} · ${ref.chars} 字，切成 ${ref.chunks} 段${ref.truncated ? '（太长，只取了前 20 万字）' : ''}`,
      );
    } catch (err) {
      log.warn('meeting', 'doc-ingest-failed', { url: link.url.slice(-30), err: String(err) });
      lines.push(`❌ ${link.url}\n   ${docErrorText(err)}`);
    }
  }
  if (links.length > DOC_MAX_LINKS) lines.push(`（一次最多 ${DOC_MAX_LINKS} 个链接，其余没处理）`);
  if (lines.some((l) => l.startsWith('✅'))) {
    if (target) {
      lines.push(
        `已绑定到会议「${target.topic ?? target.meetingNo}」（${target.meetingNo}）。会中 @ 我时会先查这些资料，回答里标出处和版本。`,
      );
    } else if (waitFor && 'meetingNo' in waitFor) {
      lines.push(`会议 ${waitFor.meetingNo} 还没开始。我进入这场会时会带上这些资料（12 小时内有效）。`);
    } else {
      lines.push(
        '现在没有进行中的会议。你在这个会话里让我入会（`/meeting join` 或转发会议卡片）时会带上这些资料，12 小时内有效。\n' +
          '如果是直接从会议里邀请我入会，请带上会议号再发一次：`会前资料 <链接> <会议号>`。',
      );
    }
  }
  await reply(lines.join('\n'));
}

/** `/meeting docs`: docs per live meeting, plus waiting ones. */
export async function replyMeetingDocs(
  ctx: Pick<MeetingDocCommandContext, 'controls' | 'sessions'>,
  reply: (markdown: string) => Promise<void>,
): Promise<void> {
  const b = loadDocBindings(meetingDataDir(ctx.controls));
  const lines: string[] = [];
  for (const s of ctx.sessions().filter((x) => !x.ended)) {
    const refs = b.meetings[s.meetingId]?.docs ?? [];
    lines.push(`**${s.topic ?? s.meetingNo}**（${s.meetingNo}）：${refs.length ? '' : '没有资料'}`);
    for (const r of refs) lines.push(`- ${docLabel(r)} · ${r.chunks} 段 · ${shortTime(r.fetchedAt)} 抓取`);
  }
  const now = Date.now();
  const fresh = b.pending.filter((r) => age(now, r.boundAt) < DOC_PENDING_TTL_MS);
  if (fresh.length) {
    lines.push('**等待中**：');
    for (const r of fresh) {
      lines.push(`- ${docLabel(r)} · ${r.chunks} 段 · ${shortTime(r.fetchedAt)} 抓取 · ${waitText(r.waitFor)}`);
    }
  }
  await reply(lines.length ? lines.join('\n') : '现在没有绑定任何会前资料。发 `/meeting doc <飞书文档链接>` 绑定。');
}
