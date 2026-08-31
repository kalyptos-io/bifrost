import { describe, it, expect } from 'vitest';
import { highlight, windowRange } from './json';

describe('highlight', () => {
	it('escapes markup before tokenizing', () => {
		const out = highlight('"<b>&</b>"');
		expect(out).toContain('&lt;b&gt;&amp;&lt;/b&gt;');
		expect(out).not.toContain('<b>');
	});
	it('classifies a key apart from a value string', () => {
		const out = highlight('{"a": "b"}');
		expect(out).toContain('<span class="k">"a"</span>:');
		expect(out).toContain('<span class="s">"b"</span>');
	});
	it('classifies literals and numbers', () => {
		expect(highlight('null')).toBe('<span class="l">null</span>');
		expect(highlight('-1.5e3')).toBe('<span class="n">-1.5e3</span>');
	});
	it('is line-safe: a slice highlights the same as the whole', () => {
		const doc = '{\n  "a": 1,\n  "b": 2\n}';
		const lines = doc.split('\n');
		expect(lines.slice(1, 3).map(highlight).join('\n')).toBe(highlight(lines.slice(1, 3).join('\n')));
	});
});

describe('windowRange', () => {
	// 10px rows, 100px viewport -> 10 visible, 10 of head and 10 of tail
	it('starts at zero at the top of the document', () => {
		expect(windowRange(0, 100, 10, 1000)).toEqual({ start: 0, end: 30 });
	});
	it('keeps one viewport of head once scrolled past it', () => {
		expect(windowRange(500, 100, 10, 1000)).toEqual({ start: 40, end: 70 });
	});
	it('clamps the tail to the document end', () => {
		expect(windowRange(9900, 100, 10, 1000)).toEqual({ start: 980, end: 1000 });
	});
	it('returns the whole of a document shorter than the viewport', () => {
		expect(windowRange(0, 100, 10, 4)).toEqual({ start: 0, end: 4 });
	});
	it('renders nothing before the row height is measurable', () => {
		expect(windowRange(0, 100, 0, 1000)).toEqual({ start: 0, end: 0 });
	});
});
