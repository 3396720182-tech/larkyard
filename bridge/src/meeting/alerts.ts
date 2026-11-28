import type { LarkChannel } from '@larksuite/channel';
import { log } from '../core/logger';
import type { MeetingAlertsConfig, ProfileConfig } from '../config/profile-schema';
import type { MeetingSession } from './session';
import type { TranscriptEvent } from './types';

/**
 * In-meeting alerts: literal keyword / speaker matches on the transcript,
 * DMed to the bot owner. No agent run is involved.
 */

const MAX_SENTENCES = 2000;
const TEXT_MAX = 200;

export interface AlertHit {
  kind: 'keyword' | 'speaker';
  value: string;
}

type AlertState = Pick<MeetingSession, 'alertFired' | 'alertCooldown'>;

/**
 * Rules this transcript line should alert on now: de-duplicated per sentence
 * and outside their cooldown. State lives on the session, so a new meeting
 * alerts afresh.
 */
export function matchMeetingAlerts(
  session: AlertState,
  cfg: MeetingAlertsConfig,
  event: Pick<TranscriptEvent, 'text' | 'speaker' | 'sentenceId'>,
  now: number,
): AlertHit[] {
  if (cfg.keywords.length === 0 && cfg.speakers.length === 0) return [];
  const text = String(event.text ?? '');
  if (!text) return [];
  const lower = text.toLowerCase();
  const speakerName = event.speaker?.name ?? '';
  const speakerId = event.speaker?.id ?? '';
  const hits: AlertHit[] = [];
  for (const k of cfg.keywords) {
    if (lower.includes(k.toLowerCase())) hits.push({ kind: 'keyword', value: k });
  }
  for (const s of cfg.speakers) {
    if (s === speakerId || (speakerName && s.toLowerCase() === speakerName.toLowerCase())) {
      hits.push({ kind: 'speaker', value: s });
    }
  }
  if (hits.length === 0) return [];

  let fired = session.alertFired.get(event.sentenceId);
  if (!fired) {
    fired = new Set();
    session.alertFired.set(event.sentenceId, fired);
    // Bounded like seenEvents: a Map iterates in insertion order, so this drops the oldest.
    if (session.alertFired.size > MAX_SENTENCES) {
      for (const key of session.alertFired.keys()) {
        session.alertFired.delete(key);
        if (session.alertFired.size <= MAX_SENTENCES * 0.75) break;
      }
    }
  }
  const out: AlertHit[] = [];
  for (const h of hits) {
    const ruleKey = `${h.kind}:${h.value}`;
    // ASR resends the same sentence with better text: one alert per sentence per rule.
    if (fired.has(ruleKey)) continue;
    const window = h.kind === 'speaker' ? cfg.speakerCooldownMs : cfg.cooldownMs;
    const last = session.alertCooldown.get(ruleKey);
    // Inside the cooldown still counts as handled: otherwise a resend after the
    // cooldown would alert about a sentence that is long gone.
    fired.add(ruleKey);
    if (window > 0 && last !== undefined && now - last < window) continue;
    session.alertCooldown.set(ruleKey, now);
    out.push(h);
  }
  return out;
}

/** Alert text: why, who, what was said, and which meeting. */
export function formatMeetingAlert(
  session: Pick<MeetingSession, 'topic' | 'meetingNo'>,
  event: Pick<TranscriptEvent, 'text' | 'speaker'>,
  hits: AlertHit[],
): string {
  const who = event.speaker?.name ?? event.speaker?.id ?? '未知发言人';
  const raw = String(event.text ?? '');
  const text = raw.length > TEXT_MAX ? `${raw.slice(0, TEXT_MAX)}…（已截断）` : raw;
  const kws = hits.filter((h) => h.kind === 'keyword').map((h) => h.value);
  const reason = kws.length > 0 ? `关键词命中：${kws.join('、')}` : '指定发言人开口';
  const title = session.topic ?? session.meetingNo;
  return `🔔 **会中提醒** · ${reason}\n会议：${title}（${session.meetingNo}）\n**${who}**：${text}`;
}

/**
 * Subscribe to the transcript and deliver alerts. Always to the owner's DM —
 * never the meeting chat or the origin group, which would reveal what is
 * being watched. No try/catch: MeetingSession.emit isolates handler errors.
 */
export function attachMeetingAlerts(deps: {
  session: MeetingSession;
  controls: { profileConfig: ProfileConfig; botOwnerId?: string };
  channel: Pick<LarkChannel, 'send'>;
  now?: () => number;
}): void {
  const { session, controls, channel } = deps;
  const now = deps.now ?? (() => Date.now());
  session.on('transcript', (e) => {
    const event = e as TranscriptEvent;
    const hits = matchMeetingAlerts(session, controls.profileConfig.meeting.alerts, event, now());
    if (hits.length === 0) return;
    session.alertHits += 1;
    log.info('meeting', 'alert', {
      meetingId: session.meetingId,
      meetingNo: session.meetingNo,
      reasons: hits.map((h) => `${h.kind}:${h.value}`).join(','),
      total: session.alertHits,
    });
    const to = controls.botOwnerId;
    if (!to) {
      log.warn('meeting', 'alert-no-target', { meetingId: session.meetingId });
      return;
    }
    void channel
      .send(to, { markdown: formatMeetingAlert(session, event, hits) })
      .catch((err) => log.warn('meeting', 'alert-send-failed', { meetingId: session.meetingId, err: String(err) }));
  });
}
