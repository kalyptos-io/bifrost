// geometry helpers for the try-it canvas. api geometry is already EPSG:25832 (easting/northing,
// metres), so this is flatten + bbox-fit only - no projection maths.

export type Pt = [number, number];
export type BBox = [number, number, number, number];

export type Geom = {
	type: string;
	coordinates?: unknown;
	geometries?: Geom[];
};

/** split a geojson geometry into polylines and standalone points. */
export function geomToParts(g: Geom | null | undefined, segs: Pt[][], pts: Pt[]): void {
	if (!g) return;
	const line = (coords: Pt[]) => {
		if (coords?.length) segs.push(coords.map((c) => [c[0], c[1]]));
	};
	switch (g.type) {
		case 'Point':
			pts.push(g.coordinates as Pt);
			break;
		case 'MultiPoint':
			(g.coordinates as Pt[]).forEach((c) => pts.push(c));
			break;
		case 'LineString':
			line(g.coordinates as Pt[]);
			break;
		case 'MultiLineString':
			(g.coordinates as Pt[][]).forEach(line);
			break;
		case 'Polygon':
			(g.coordinates as Pt[][]).forEach(line);
			break;
		case 'MultiPolygon':
			(g.coordinates as Pt[][][]).forEach((poly) => poly.forEach(line));
			break;
		case 'GeometryCollection':
			g.geometries?.forEach((gg) => geomToParts(gg, segs, pts));
			break;
	}
}

/** bbox over every drawn vertex; null when there is nothing to draw. */
export function bounds(segs: Pt[][], pts: Pt[]): BBox | null {
	let minx = Infinity,
		miny = Infinity,
		maxx = -Infinity,
		maxy = -Infinity;
	const seen = (p: Pt) => {
		minx = Math.min(minx, p[0]);
		miny = Math.min(miny, p[1]);
		maxx = Math.max(maxx, p[0]);
		maxy = Math.max(maxy, p[1]);
	};
	segs.forEach((s) => s.forEach(seen));
	pts.forEach(seen);
	return minx === Infinity ? null : [minx, miny, maxx, maxy];
}

/** map a bbox into a w*h canvas, northing up, aspect preserved. a zero span centres. */
export function fit(b: BBox, w: number, h: number, pad = 16): (p: Pt) => Pt {
	const [minx, miny, maxx, maxy] = b;
	const dx = maxx - minx,
		dy = maxy - miny;
	const s = Math.min((w - 2 * pad) / (dx || 1), (h - 2 * pad) / (dy || 1));
	const cx = (minx + maxx) / 2,
		cy = (miny + maxy) / 2;
	return (p: Pt) => [w / 2 + (p[0] - cx) * s, h / 2 - (p[1] - cy) * s];
}
