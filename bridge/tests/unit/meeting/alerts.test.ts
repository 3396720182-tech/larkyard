import { describe, expect, it } from 'vitest';
import { formatMeetingAlert, matchMeetingAlerts } from '../../../src/meeting/alerts';
import { normalizeMeetingAlerts } from '../../../src/config/profile-schema';

const state = () => ({ alertFired: new Map<string, Set<string>>(), alertCooldown: new Map<string, number>() });
const cfg = (over = {}) => ({ keywords: [], speakers: [], cooldownMs: 60_000, speakerCooldownMs: 300_000, ...over });
const line = (sentenceId: string, text: string, speaker = { id: 'ou_a', name: '甲' }) => ({ sentenceId, text, speaker });

describe('matchMeetingAlerts', () => {
  it('matches keywords case-insensitively and speakers by id or name', () => {
    const s = state();
    expect(matchMeetingAlerts(s, cfg({ keywords: ['Deadline'] }), line('1', 'the deadline moved'), 0)).toEqual([
      { kind: 'keyword', value: 'Deadline' },
    ]);
    expect(matchMeetingAlerts(s, cfg({ speakers: ['甲'] }), line('2', '你好'), 0)).toEqual([{ kind: 'speaker', value: '甲' }]);
    expect(matchMeetingAlerts(s, cfg({ speakers: ['ou_a'] }), line('3', '你好'), 0)).toHaveLength(1);
  });

  it('one alert per sentence per rule, even when ASR resends the sentence', () => {
    const s = state();
    const c = cfg({ keywords: ['上线'], cooldownMs: 0 });
    expect(matchMeetingAlerts(s, c, line('1', '周五上线'), 0)).toHaveLength(1);
    expect(matchMeetingAlerts(s, c, line('1', '周五上线吧'), 1)).toHaveLength(0);
    expect(matchMeetingAlerts(s, c, line('2', '下周上线'), 2)).toHaveLength(1);
  });

  it('respects the cooldown and does not alert later about a sentence seen during it', () => {
    const s = state();
    const c = cfg({ keywords: ['上线'] });
    expect(matchMeetingAlerts(s, c, line('1', '上线'), 0)).toHaveLength(1);
    expect(matchMeetingAlerts(s, c, line('2', '上线'), 1_000)).toHaveLength(0);
    expect(matchMeetingAlerts(s, c, line('2', '上线了'), 61_000)).toHaveLength(0);
    expect(matchMeetingAlerts(s, c, line('3', '上线'), 61_000)).toHaveLength(1);
  });

  it('nothing configured = nothing matched', () => {
    expect(matchMeetingAlerts(state(), cfg(), line('1', 'x'), 0)).toEqual([]);
  });
});

describe('alerts config and text', () => {
  it('drops blank entries (a blank keyword would match every line)', () => {
    const n = normalizeMeetingAlerts({ keywords: [' ', '上线', '上线', 3], speakers: [''], cooldownMs: -5 });
    expect(n.keywords).toEqual(['上线']);
    expect(n.speakers).toEqual([]);
    expect(n.cooldownMs).toBe(0);
    expect(n.speakerCooldownMs).toBe(300_000);
  });

  it('formats reason, speaker, truncated text and the meeting', () => {
    const t = formatMeetingAlert({ meetingNo: '123456789', topic: '周会' }, line('1', 'x'.repeat(300)), [
      { kind: 'keyword', value: '上线' },
    ]);
    expect(t).toContain('关键词命中：上线');
    expect(t).toContain('周会（123456789）');
    expect(t).toContain('（已截断）');
  });
});
