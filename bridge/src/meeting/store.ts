import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { resolveAppPaths } from '../config/app-paths';
import { log } from '../core/logger';

/**
 * Files the meeting features keep across restarts: minutes archives, document
 * snapshots, document bindings, and the answer log. All live under
 * `<profileDir>/meeting-data/`; nothing here holds transcript or chat text.
 */
export function meetingDataDir(controls: { configPath: string; profile: string }): string {
  const { profileDir } = resolveAppPaths({
    rootDir: dirname(controls.configPath),
    profile: controls.profile,
  });
  return join(profileDir, 'meeting-data');
}

/**
 * Read a JSON file. A missing file is `fallback`; any other failure throws when
 * `strict`, otherwise it is logged and treated as `fallback`.
 */
export function readMeetingJson<T>(file: string, fallback: T, strict = false): T {
  try {
    return JSON.parse(readFileSync(file, 'utf8')) as T;
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') return fallback;
    if (strict) throw err;
    log.warn('meeting', 'data-read-failed', { file: file.slice(-60), err: String(err) });
    return fallback;
  }
}

/** Write JSON atomically (temp file + rename), creating the directory. */
export function writeMeetingJson(file: string, data: unknown): void {
  mkdirSync(dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n');
  renameSync(tmp, file);
}
