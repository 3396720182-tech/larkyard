import { describe, expect, it } from 'vitest';
import { classifyIntent } from '../../../src/larkyard/ticket-fast-path';

describe('classifyIntent', () => {
  it('maps a message that is exactly a control word to that control', () => {
    expect(classifyIntent('停')).toBe('stop');
    expect(classifyIntent('不做了')).toBe('stop');
    expect(classifyIntent('先别动')).toBe('hold');
    expect(classifyIntent('继续')).toBe('continue');
    expect(classifyIntent('恢复')).toBe('continue');
    expect(classifyIntent('执行')).toBe('approve');
    expect(classifyIntent('批准')).toBe('approve');
    expect(classifyIntent('换新会话重来')).toBe('restart');
  });

  it('ignores a leading @mention and trailing punctuation', () => {
    expect(classifyIntent('@Larkyard 停！')).toBe('stop');
    expect(classifyIntent('  继续。 ')).toBe('continue');
  });

  it('treats anything longer as a follow-up for the worker', () => {
    expect(classifyIntent('停一下，先把第二张图改成竖版')).toBe('followup');
    expect(classifyIntent('继续把剩下的三镜出完')).toBe('followup');
    expect(classifyIntent('')).toBe('followup');
  });
});

import { routeTicketMessage, type TicketResult } from '../../../src/larkyard/ticket-fast-path';

/** Fake `ticket` CLI: records every call and answers from a script keyed by sub-command. */
function fakeTicket(answers: Record<string, TicketResult | undefined>) {
  const calls: string[][] = [];
  const run = async (args: string[]): Promise<TicketResult | undefined> => {
    calls.push(args);
    return answers[args[0]!];
  };
  return { calls, run };
}

const ok = (stdout: string): TicketResult => ({ code: 0, stdout, stderr: '' });
const miss: TicketResult = { code: 1, stdout: '', stderr: '' };
const err = (stderr: string): TicketResult => ({ code: 2, stdout: '', stderr });

describe('routeTicketMessage', () => {
  it('leaves messages without a quote or ticket id to the bridge session', async () => {
    const t = fakeTicket({});
    const out = await routeTicketMessage({ messageId: 'om_2', content: '帮我写个脚本', hasResources: false }, t.run);
    expect(out).toBeUndefined();
    expect(t.calls).toEqual([]);
  });

  it('leaves quotes of non-ticket messages to the bridge session', async () => {
    const t = fakeTicket({ lookup: miss });
    const out = await routeTicketMessage(
      { messageId: 'om_2', content: '这个怎么看', quotedId: 'om_1', hasResources: false }, t.run);
    expect(out).toBeUndefined();
    expect(t.calls).toEqual([['lookup', '--message', 'om_1']]);
  });

  it('forwards a follow-up on a quoted ticket message straight to the ticket', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), send: ok('📨 已转给 T0005\n') });
    const out = await routeTicketMessage(
      { messageId: 'om_2', content: '第二张改成竖版', quotedId: 'om_1', hasResources: false }, t.run);
    expect(t.calls).toEqual([
      ['lookup', '--message', 'om_1'],
      ['send', 'T0005', '--message', 'om_2', '--text', '第二张改成竖版'],
    ]);
    expect(out).toEqual({ ticket: 'T0005', action: 'send', reply: '📨 已转给 T0005' });
  });

  it('leaves messages with attachments to the bridge session', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n') });
    const out = await routeTicketMessage(
      { messageId: 'om_2', content: '用这张图', quotedId: 'om_1', hasResources: true }, t.run);
    expect(out).toBeUndefined();
    expect(t.calls).toEqual([]);
  });
});

describe('routeTicketMessage: control words on a quoted ticket message', () => {
  const quoted = (content: string) => ({ messageId: 'om_2', content, quotedId: 'om_1', hasResources: false });

  it.each([
    ['停', 'stop'],
    ['先别动', 'hold'],
    ['换新会话重来', 'restart'],
  ])('%s runs ticket %s', async (word, cmd) => {
    const t = fakeTicket({ lookup: ok('T0005\n'), [cmd]: ok(`done ${cmd}\n`) });
    const out = await routeTicketMessage(quoted(word), t.run);
    expect(t.calls.at(-1)).toEqual([cmd, 'T0005']);
    expect(out).toEqual({ ticket: 'T0005', action: cmd, reply: `done ${cmd}` });
  });

  it('继续 releases a held or stopped ticket', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), release: ok('▶️ 已放开 T0005\n') });
    const out = await routeTicketMessage(quoted('继续'), t.run);
    expect(t.calls.at(-1)).toEqual(['release', 'T0005']);
    expect(out?.reply).toBe('▶️ 已放开 T0005');
  });

  it('继续 on a ticket that is neither held nor stopped is a follow-up', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), release: err('错误：T0005 不在暂停中，也没有停止\n'), send: ok('📨 已转给 T0005\n') });
    const out = await routeTicketMessage(quoted('继续'), t.run);
    expect(t.calls.at(-1)).toEqual(['send', 'T0005', '--message', 'om_2', '--text', '继续']);
    expect(out).toEqual({ ticket: 'T0005', action: 'send', reply: '📨 已转给 T0005' });
  });

  it('执行 approves a pending out-of-sandbox run', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), approve: ok('▶️ 已批准 T0005 的沙箱外执行（R3）\n') });
    const out = await routeTicketMessage(quoted('执行'), t.run);
    expect(t.calls.at(-1)).toEqual(['approve', 'T0005']);
    expect(out?.action).toBe('approve');
  });

  it('执行 with nothing to approve is a follow-up', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), approve: err('错误：T0005 没有待批准的沙箱外执行\n'), send: ok('📨 已转给 T0005\n') });
    const out = await routeTicketMessage(quoted('执行'), t.run);
    expect(t.calls.at(-1)?.[0]).toBe('send');
    expect(out?.action).toBe('send');
  });

  it('relays the CLI error when the command is refused', async () => {
    const t = fakeTicket({ lookup: ok('T0005\n'), send: err('错误：T0005 已停止；要接着做先恢复（ticket release T0005），或者开新单\n') });
    const out = await routeTicketMessage(quoted('再改一下'), t.run);
    expect(out).toEqual({ ticket: 'T0005', action: 'send', reply: '错误：T0005 已停止；要接着做先恢复（ticket release T0005），或者开新单' });
  });
});

describe('routeTicketMessage: control word with an explicit ticket id', () => {
  const plain = (content: string) => ({ messageId: 'om_2', content, hasResources: false });

  it('runs the control on the named ticket without a lookup', async () => {
    const t = fakeTicket({ stop: ok('⏹️ 已停止 T0005\n') });
    const out = await routeTicketMessage(plain('停 T0005'), t.run);
    expect(t.calls).toEqual([['stop', 'T0005']]);
    expect(out).toEqual({ ticket: 'T0005', action: 'stop', reply: '⏹️ 已停止 T0005' });
  });

  it('accepts the id glued to the word and in lower case', async () => {
    const t = fakeTicket({ hold: ok('⏸️ 已暂停 T0012\n') });
    await routeTicketMessage(plain('先别动t0012'), t.run);
    expect(t.calls).toEqual([['hold', 'T0012']]);
  });

  it('leaves a refused 继续 / 执行 without a quote to the bridge session', async () => {
    const t = fakeTicket({ release: err('错误：T0005 不在暂停中，也没有停止\n') });
    expect(await routeTicketMessage(plain('继续 T0005'), t.run)).toBeUndefined();
    expect(t.calls).toEqual([['release', 'T0005']]);
  });

  it('leaves other messages that mention a ticket id to the bridge session', async () => {
    const t = fakeTicket({});
    expect(await routeTicketMessage(plain('T0005 现在怎么样了'), t.run)).toBeUndefined();
    expect(t.calls).toEqual([]);
  });
});

describe('routeTicketMessage: when the ticket CLI cannot run', () => {
  it('leaves the message to the bridge session', async () => {
    const t = fakeTicket({ lookup: undefined });
    const out = await routeTicketMessage(
      { messageId: 'om_2', content: '再改一下', quotedId: 'om_1', hasResources: false }, t.run);
    expect(out).toBeUndefined();
  });
});

import { mkdtemp, rm, writeFile, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach } from 'vitest';
import { createTicketRunner } from '../../../src/larkyard/ticket-fast-path';

describe('createTicketRunner (real process)', () => {
  const dirs: string[] = [];
  afterEach(async () => {
    await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
  });

  async function script(body: string): Promise<string> {
    const dir = await mkdtemp(join(tmpdir(), 'ticket-runner-'));
    dirs.push(dir);
    const file = join(dir, 'ticket');
    await writeFile(file, `#!${process.execPath}\n${body}\n`);
    await chmod(file, 0o755);
    return file;
  }

  it('returns exit code, stdout and stderr of the ticket CLI', async () => {
    const bin = await script(
      `const a = process.argv.slice(2); process.stdout.write('args:' + a.join('|')); process.stderr.write('e'); process.exit(a[0] === 'lookup' ? 1 : 0);`,
    );
    const run = createTicketRunner(bin);
    expect(await run(['send', 'T0001', '--text', 'a b'])).toEqual({ code: 0, stdout: 'args:send|T0001|--text|a b', stderr: 'e' });
    expect((await run(['lookup', '--message', 'om_1']))?.code).toBe(1);
  });

  it('resolves undefined when the binary is missing', async () => {
    const run = createTicketRunner('/nonexistent/ticket');
    expect(await run(['ls'])).toBeUndefined();
  });

  it('kills a hung call and resolves undefined after the timeout', async () => {
    const bin = await script(`setTimeout(() => {}, 60000);`);
    const run = createTicketRunner(bin, 300);
    const started = Date.now();
    expect(await run(['ls'])).toBeUndefined();
    expect(Date.now() - started).toBeLessThan(5000);
  });
});
