import { describe, it, expect, afterEach, vi, type Mock } from 'vitest';
import { resolve, search } from './api';

function stubFetch(): Mock {
	const mock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ query: '', matches: [] }) });
	vi.stubGlobal('fetch', mock);
	return mock;
}

function sentBody(mock: Mock): Record<string, unknown> {
	const init = mock.mock.calls[0][1] as RequestInit;
	return JSON.parse(init.body as string);
}

afterEach(() => vi.unstubAllGlobals());

describe('resolve() lifecycle body', () => {
	it('omits lifecycle when unset', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto' });
		expect(sentBody(f)).not.toHaveProperty('lifecycle');
	});
	it('omits lifecycle when exactly the default ["current"]', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto', lifecycle: ['current'] });
		expect(sentBody(f)).not.toHaveProperty('lifecycle');
	});
	it('sends lifecycle when a non-default set is selected', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto', lifecycle: ['current', 'retired'] });
		expect(sentBody(f).lifecycle).toEqual(['current', 'retired']);
	});
	it('sends lifecycle when current is deselected', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto', lifecycle: ['retired'] });
		expect(sentBody(f).lifecycle).toEqual(['retired']);
	});
});

describe('resolve() limit body', () => {
	it('defaults to 5', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto' });
		expect(sentBody(f).limit).toBe(5);
	});
	it('clamps to the 1..20 the API accepts', async () => {
		const f = stubFetch();
		await resolve({ query: 'x', target: 'auto', limit: 50 });
		expect(sentBody(f).limit).toBe(20);
	});
});

describe('search() lifecycle body', () => {
	it('omits lifecycle when exactly the default ["current"]', async () => {
		const f = stubFetch();
		await search({ query: 'x', target: 'street', limit: 5, lifecycle: ['current'] });
		expect(sentBody(f)).not.toHaveProperty('lifecycle');
	});
	it('sends lifecycle when a non-default set is selected', async () => {
		const f = stubFetch();
		await search({ query: 'x', target: 'street', limit: 5, lifecycle: ['preliminary', 'abandoned'] });
		expect(sentBody(f).lifecycle).toEqual(['preliminary', 'abandoned']);
	});
});
