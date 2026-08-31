// bifrost API client. mirrors app/src/bifrost/api/contract.py.
// base resolves at call time from window.__BIFROST_API__ (runtime-injected via /env.js),
// empty -> relative same-origin.

import type { Geom } from './geo';

export const RESOLVE_TARGETS = [
	'address',
	'auto',
	'street',
	'postcode',
	'city',
	'ejendom',
	'kommune',
	'sogn',
	'region',
	'retskreds',
	'politikreds',
	'opstillingskreds'
] as const;

export const SEARCH_TARGETS = [
	'street',
	'postcode',
	'city',
	'ejendom',
	'kommune',
	'sogn',
	'region',
	'retskreds',
	'politikreds',
	'opstillingskreds',
	'stednavne'
] as const;

export const COMPONENT_FIELDS = [
	'street',
	'house_number',
	'house_letter',
	'floor',
	'door',
	'postcode',
	'city',
	'sub_locality'
] as const;

export const LIFECYCLE_STATES = ['current', 'retired', 'preliminary', 'abandoned'] as const;

export type ResolveTarget = (typeof RESOLVE_TARGETS)[number];
export type SearchTarget = (typeof SEARCH_TARGETS)[number];
export type ComponentField = (typeof COMPONENT_FIELDS)[number];
export type Lifecycle = (typeof LIFECYCLE_STATES)[number];

export interface Geometry {
	srid: number;
	geojson: Geom;
	vejpunkt: [number, number] | null;
}

export interface Meta {
	score: number;
	confidence: string;
	uuid: string | null;
}

export type EjendomType = 'samlet_fast_ejendom' | 'ejerlejlighed' | 'bygning_paa_fremmed_grund';

export interface PropertyRef {
	bfe: string;
	type: string;
}

// parent ancestry (nearest -> ground, self excluded) + direct children; only on ejendom-kind matches
export interface RefContainer {
	refs: PropertyRef[];
}

export interface ParentRelations extends RefContainer {
	complete: boolean;
}

export interface Ejendom {
	bfe: string;
	type: EjendomType;
	ejerlejlighedsnummer?: string;
	relations: {
		parents: ParentRelations;
		children: RefContainer;
	};
}

export interface Match {
	kind: string;
	result: string;
	components: Record<string, string>;
	postcodes: string[] | null;
	geometry: Geometry | null;
	ejendom?: Ejendom;
	meta: Meta;
	lifecycle?: string; // omitted by older API; UI tolerates absence
}

export interface AddressResult {
	query: string;
	matches: Match[];
}

export class ApiError extends Error {
	constructor(
		public status: number,
		public statusText: string
	) {
		super(`HTTP ${status} · ${statusText}`);
	}
}

function apiBase(): string {
	const b = typeof window !== 'undefined' ? window.__BIFROST_API__ : '';
	return (b ?? '').replace(/\/$/, '');
}

async function post(endpoint: string, body: unknown): Promise<AddressResult> {
	const r = await fetch(apiBase() + endpoint, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!r.ok) throw new ApiError(r.status, r.statusText);
	return (await r.json()) as AddressResult;
}

// omit when exactly the server default; send the array otherwise
function wireLifecycle(lifecycle?: Lifecycle[]): Lifecycle[] | undefined {
	if (!lifecycle || (lifecycle.length === 1 && lifecycle[0] === 'current')) return undefined;
	return lifecycle;
}

export function resolve(opts: {
	query?: string;
	components?: Record<string, string>;
	target: ResolveTarget;
	lifecycle?: Lifecycle[];
	geometry?: boolean;
	uuid?: boolean;
	limit?: number;
}): Promise<AddressResult> {
	const body: Record<string, unknown> = {
		project: opts.target,
		geometry: opts.geometry ?? true,
		uuid: opts.uuid ?? false,
		limit: Math.min(20, Math.max(1, opts.limit || 5)) // AddressRequest rejects out of 1..20
	};
	if (opts.query) body.query = opts.query;
	if (opts.components && Object.keys(opts.components).length) body.components = opts.components;
	const lifecycle = wireLifecycle(opts.lifecycle);
	if (lifecycle) body.lifecycle = lifecycle;
	return post('/resolve', body);
}

export function search(opts: {
	query: string;
	target: SearchTarget;
	limit: number;
	lifecycle?: Lifecycle[];
	geometry?: boolean;
}): Promise<AddressResult> {
	const limit = Math.min(100, Math.max(1, opts.limit || 5)); // SearchRequest rejects out of 1..100
	const body: Record<string, unknown> = {
		query: opts.query,
		target: opts.target,
		geometry: opts.geometry ?? false,
		limit
	};
	const lifecycle = wireLifecycle(opts.lifecycle);
	if (lifecycle) body.lifecycle = lifecycle;
	return post('/search', body);
}
