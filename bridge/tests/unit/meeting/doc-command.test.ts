import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NormalizedMessage } from '@larksuite/channel';
import { runCommandHandler, tryHandleCommand, type CommandContext } from '../../../src/commands/index';
import { createDefaultProfileConfig } from '../../../src/config/profile-schema';
import { loadDocBindings } from '../../../src/meeting/docs';
import { meetingDataDir } from '../../../src/meeting/store';
import { createFakeChannel } from '../../helpers/fake-channel';

const LINK = 'https://x.feishu.cn/docx/doxcnAAAAAAAAAAAAAAAA1'; // check-private: ignore

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'meeting-doc-cmd-'));
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

function harness(opts: { senderId?: string; chatType?: string; content: string }) {
  const channel = createFakeChannel() as ReturnType<typeof createFakeChannel> & { rawClient: unknown };
  const request = vi.fn(async ({ url }: { url: string }) =>
    url.endsWith('/raw_content')
      ? { code: 0, data: { content: '正文' } }
      : { code: 0, data: { document: { title: '方案', revision_id: 12 } } },
  );
  channel.rawClient = { request } as never;
  const profileConfig = createDefaultProfileConfig({
    agentKind: 'claude',
    accounts: { app: { id: 'app-id', secret: 'secret', tenant: 'feishu' } },
    access: { admins: ['ou-admin'] },
  });
  profileConfig.meeting = { ...profileConfig.meeting, enabled: true };
  const controls = {
    profile: 'claude',
    configPath: join(root, 'config.json'),
    profileConfig,
    botOwnerId: 'ou-owner',
    ownerRefreshState: 'ok',
    meeting: { all: () => [] },
  };
  const ctx = {
    channel,
    msg: {
      messageId: 'om-1',
      chatId: 'oc-dm',
      chatType: opts.chatType ?? 'p2p',
      senderId: opts.senderId ?? 'ou-owner',
      content: opts.content,
      resources: [],
      mentions: [],
      mentionedBot: false,
    } as unknown as NormalizedMessage,
    scope: 'oc-dm',
    chatMode: 'p2p',
    controls,
  } as unknown as CommandContext;
  const replies = () => channel.sent.map((m) => String((m.content as { markdown?: string }).markdown ?? ''));
  const pending = () => loadDocBindings(meetingDataDir(controls)).pending;
  return { ctx, request, replies, pending, channel };
}

describe('会前资料 in a DM', () => {
  it('an admin message starting with 会前资料 binds the doc', async () => {
    const h = harness({ content: `会前资料 ${LINK}` });
    await expect(tryHandleCommand(h.ctx)).resolves.toBe(true);
    expect(h.pending()).toHaveLength(1);
    expect(h.replies()[0]).toContain('《方案》r12');
  });

  it('only at the start, only in a DM, only for admins', async () => {
    for (const h of [
      harness({ content: `帮我总结这份会前资料 ${LINK}` }),
      harness({ content: `会前资料 ${LINK}`, chatType: 'group' }),
      harness({ content: `会前资料 ${LINK}`, senderId: 'ou-stranger' }),
    ]) {
      await expect(tryHandleCommand(h.ctx)).resolves.toBe(false);
      expect(h.request).not.toHaveBeenCalled();
    }
  });
});

describe('派成工单 button', () => {
  it('a non-admin click does nothing', async () => {
    const h = harness({ content: '', senderId: 'ou-stranger' });
    await runCommandHandler('meeting', 'todo 20261121-1430-123456789#1', h.ctx);
    expect(h.channel.sent).toHaveLength(0);
  });

  it('an admin click with an unknown archive says so', async () => {
    const h = harness({ content: '', senderId: 'ou-admin' });
    await runCommandHandler('meeting', 'todo 20261121-1430-123456789#1', h.ctx);
    expect(h.replies()[0]).toContain('找不到这条待办');
  });
});
