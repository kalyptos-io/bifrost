import { describe, it, expect } from 'vitest';
import { resolveRequest, searchRequest } from './api';

describe('resolveRequest() lifecycle body', () => {
	it('omits lifecycle when unset', () => {
		expect(resolveRequest({ query: 'x', target: 'auto' }).body).not.toHaveProperty('lifecycle');
	});
	it('omits lifecycle when exactly the default ["current"]', () => {
		const body = resolveRequest({ query: 'x', target: 'auto', lifecycle: ['current'] }).body;
		expect(body).not.toHaveProperty('lifecycle');
	});
	it('sends lifecycle when a non-default set is selected', () => {
		const body = resolveRequest({ query: 'x', target: 'auto', lifecycle: ['current', 'retired'] }).body;
		expect(body.lifecycle).toEqual(['current', 'retired']);
	});
	it('sends lifecycle when current is deselected', () => {
		const body = resolveRequest({ query: 'x', target: 'auto', lifecycle: ['retired'] }).body;
		expect(body.lifecycle).toEqual(['retired']);
	});
});

describe('resolveRequest() limit body', () => {
	it('defaults to 5', () => {
		expect(resolveRequest({ query: 'x', target: 'auto' }).body.limit).toBe(5);
	});
	it('clamps to the 1..20 the API accepts', () => {
		expect(resolveRequest({ query: 'x', target: 'auto', limit: 50 }).body.limit).toBe(20);
	});
});

describe('searchRequest() lifecycle body', () => {
	it('omits lifecycle when exactly the default ["current"]', () => {
		const body = searchRequest({ query: 'x', target: 'street', limit: 5, lifecycle: ['current'] }).body;
		expect(body).not.toHaveProperty('lifecycle');
	});
	it('sends lifecycle when a non-default set is selected', () => {
		const body = searchRequest({
			query: 'x',
			target: 'street',
			limit: 5,
			lifecycle: ['preliminary', 'abandoned']
		}).body;
		expect(body.lifecycle).toEqual(['preliminary', 'abandoned']);
	});
});
