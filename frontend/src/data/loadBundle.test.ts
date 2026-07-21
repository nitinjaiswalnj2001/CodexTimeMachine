import { describe, expect, it, vi } from 'vitest';
import { loadBundle } from './loadBundle';

describe('accepted demo bundle loading', () => {
  it('rejects malformed evidence without inventing values', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ artifacts: {} }) }));
    await expect(loadBundle()).rejects.toThrow('missing required accepted artifacts');
  });

  it('loads required accepted artifacts', async () => {
    const bundle = { bundle_schema_version: '1', demo_id: 'demo', title: 'Demo', artifacts: { trajectory: {}, replay_trajectory: {}, intervention: {}, divergence: {}, counterfactual: {}, assessment: {}, evaluation: {}, phase_8_receipt: {}, replay_manifest: {} } };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => bundle }));
    await expect(loadBundle()).resolves.toEqual(bundle);
  });
});
