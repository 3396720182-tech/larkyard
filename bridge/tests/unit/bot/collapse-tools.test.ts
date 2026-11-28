import { afterEach, describe, expect, it, vi } from 'vitest';
import { collapseToolBlocks } from '../../../src/bot/channel';
import { initialState, type RunState } from '../../../src/card/run-state';

const tool = { kind: 'tool', name: 'Bash' } as unknown as RunState['blocks'][number];
const text = (content: string) => ({ kind: 'text', content, streaming: false }) as RunState['blocks'][number];
const state = (terminal: RunState['terminal'], blocks: RunState['blocks']): RunState => ({ ...initialState, terminal, blocks });

describe('collapseToolBlocks', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('hides tools entirely when tool calls are off', () => {
    expect(collapseToolBlocks(state('running', [tool, text('a')]), false).blocks).toEqual([text('a')]);
  });

  it('keeps tool lines while running, folds them into a count once terminal', () => {
    const running = state('running', [tool, text('a')]);
    expect(collapseToolBlocks(running, true)).toBe(running);
    const done = collapseToolBlocks(state('done', [tool, tool, text('答案')]), true);
    expect(done.blocks).toEqual([text('答案'), { kind: 'text', content: '_🧰 2 次工具调用已折叠_', streaming: false }]);
  });

  it('keeps them when the run has no text, or when LARK_KEEP_TOOL_CALLS is set', () => {
    const toolsOnly = state('done', [tool]);
    expect(collapseToolBlocks(toolsOnly, true)).toBe(toolsOnly);
    vi.stubEnv('LARK_KEEP_TOOL_CALLS', '1');
    const s = state('done', [tool, text('a')]);
    expect(collapseToolBlocks(s, true)).toBe(s);
  });
});
