import { describe, expect, it } from 'vitest';
import { bounds, fit, geomToParts, type Pt } from './geom';

const parts = (g: unknown) => {
	const segs: Pt[][] = [],
		pts: Pt[] = [];
	geomToParts(g as never, segs, pts);
	return { segs, pts };
};

describe('geomToParts', () => {
	it('splits every geometry type the api emits', () => {
		expect(parts({ type: 'Point', coordinates: [1, 2] })).toEqual({ segs: [], pts: [[1, 2]] });
		expect(parts({ type: 'MultiPoint', coordinates: [[1, 2]] }).pts).toEqual([[1, 2]]);
		expect(
			parts({
				type: 'LineString',
				coordinates: [
					[0, 0],
					[1, 1]
				]
			}).segs
		).toHaveLength(1);
		expect(
			parts({
				type: 'MultiLineString',
				coordinates: [
					[
						[0, 0],
						[1, 1]
					],
					[
						[2, 2],
						[3, 3]
					]
				]
			}).segs
		).toHaveLength(2);
		// polygon rings (outer + hole) each become a polyline
		expect(
			parts({
				type: 'Polygon',
				coordinates: [
					[
						[0, 0],
						[1, 0],
						[0, 1],
						[0, 0]
					],
					[
						[0.2, 0.2],
						[0.4, 0.2],
						[0.2, 0.4],
						[0.2, 0.2]
					]
				]
			}).segs
		).toHaveLength(2);
		expect(
			parts({
				type: 'MultiPolygon',
				coordinates: [
					[
						[
							[0, 0],
							[1, 0],
							[0, 1],
							[0, 0]
						]
					],
					[
						[
							[5, 5],
							[6, 5],
							[5, 6],
							[5, 5]
						]
					]
				]
			}).segs
		).toHaveLength(2);
		const gc = parts({
			type: 'GeometryCollection',
			geometries: [
				{ type: 'Point', coordinates: [1, 2] },
				{
					type: 'LineString',
					coordinates: [
						[0, 0],
						[1, 1]
					]
				}
			]
		});
		expect([gc.pts.length, gc.segs.length]).toEqual([1, 1]);
	});

	it('ignores null and unknown geometry', () => {
		expect(parts(null)).toEqual({ segs: [], pts: [] });
		expect(parts({ type: 'Nonsense', coordinates: [1, 2] })).toEqual({ segs: [], pts: [] });
	});
});

describe('bounds', () => {
	it('spans segments and points', () => {
		expect(
			bounds(
				[
					[
						[0, 0],
						[10, 4]
					]
				],
				[[-2, 7]]
			)
		).toEqual([-2, 0, 10, 7]);
	});

	it('is null with nothing to draw', () => {
		expect(bounds([], [])).toBeNull();
	});
});

describe('fit', () => {
	it('maps a bbox into the canvas with northing up', () => {
		const project = fit([0, 0, 100, 100], 200, 200, 20);
		expect(project([50, 50])).toEqual([100, 100]);
		const [x, y] = project([0, 0]);
		expect(x).toBeCloseTo(20);
		expect(y).toBeCloseTo(180); // min northing lands at the bottom
	});

	it('centres a degenerate single-point bbox', () => {
		expect(fit([5, 5, 5, 5], 200, 100)([5, 5])).toEqual([100, 50]);
	});
});
