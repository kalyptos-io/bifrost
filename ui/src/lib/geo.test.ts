import { describe, it, expect } from 'vitest';
import { lonLatToUTM, simplifyRing, type Pt } from './geo';

describe('lonLatToUTM (EPSG:25832)', () => {
	it('maps the central meridian (9°E) to easting 500000', () => {
		expect(Math.abs(lonLatToUTM(9, 55)[0] - 500000)).toBeLessThan(1e-3);
	});
	it('maps 9°E, 0°N to the false origin', () => {
		const [e, n] = lonLatToUTM(9, 0);
		expect(Math.abs(e - 500000)).toBeLessThan(1e-3);
		expect(Math.abs(n)).toBeLessThan(1e-3);
	});
	it('northing grows with latitude; easting straddles the meridian', () => {
		expect(lonLatToUTM(9, 56)[1]).toBeGreaterThan(lonLatToUTM(9, 55)[1]);
		expect(lonLatToUTM(10, 55)[0]).toBeGreaterThan(500000);
		expect(lonLatToUTM(8, 55)[0]).toBeLessThan(500000);
	});
	it('places Copenhagen in the expected UTM32N band', () => {
		const [e, n] = lonLatToUTM(12.5683, 55.6761);
		expect(e).toBeGreaterThan(700000);
		expect(e).toBeLessThan(740000);
		expect(n).toBeGreaterThan(6160000);
		expect(n).toBeLessThan(6190000);
	});
});

describe('simplifyRing', () => {
	it('drops sub-tolerance points, keeps endpoints', () => {
		const line: Pt[] = [
			[0, 0],
			[1, 0],
			[2, 0],
			[100, 0]
		];
		const out = simplifyRing(line, 40);
		expect(out[0]).toEqual([0, 0]);
		expect(out[out.length - 1]).toEqual([100, 0]);
		expect(out.length).toBeLessThan(line.length);
	});
	it('returns rings shorter than 3 points unchanged', () => {
		const r: Pt[] = [
			[0, 0],
			[1, 1]
		];
		expect(simplifyRing(r, 40)).toBe(r);
	});
});
