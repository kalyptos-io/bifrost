// pure geo helpers: WGS84 backdrop projection + geojson flattening.
// result geometry from the API is already EPSG:25832 (easting/northing, metres) and
// is consumed directly; only the lon/lat backdrop is projected here.

export type Pt = [number, number];

export type Geom = {
	type: string;
	coordinates?: unknown;
	geometries?: Geom[];
};
export type GeoJSON =
	| Geom
	| { type: 'Feature'; geometry: Geom | null }
	| { type: 'FeatureCollection'; features: { type: 'Feature'; geometry: Geom | null }[] };

// WGS84 lon/lat -> EPSG:25832 (UTM zone 32N), easting/northing in metres
export function lonLatToUTM(lon: number, lat: number): Pt {
	const a = 6378137,
		f = 1 / 298.257223563,
		e2 = f * (2 - f),
		ep2 = e2 / (1 - e2),
		k0 = 0.9996,
		E0 = 500000,
		lon0 = (9 * Math.PI) / 180;
	const phi = (lat * Math.PI) / 180,
		lam = (lon * Math.PI) / 180;
	const s = Math.sin(phi),
		c = Math.cos(phi),
		t = Math.tan(phi);
	const N = a / Math.sqrt(1 - e2 * s * s),
		T = t * t,
		C = ep2 * c * c,
		A = (lam - lon0) * c;
	const M =
		a *
		((1 - e2 / 4 - (3 * e2 * e2) / 64 - (5 * e2 * e2 * e2) / 256) * phi -
			((3 * e2) / 8 + (3 * e2 * e2) / 32 + (45 * e2 * e2 * e2) / 1024) * Math.sin(2 * phi) +
			((15 * e2 * e2) / 256 + (45 * e2 * e2 * e2) / 1024) * Math.sin(4 * phi) -
			((35 * e2 * e2 * e2) / 3072) * Math.sin(6 * phi));
	const E =
		E0 +
		k0 *
			N *
			(A +
				((1 - T + C) * A * A * A) / 6 +
				((5 - 18 * T + T * T + 72 * C - 58 * ep2) * Math.pow(A, 5)) / 120);
	const Nn =
		k0 *
		(M +
			N *
				t *
				((A * A) / 2 +
					((5 - T + 9 * C + 4 * C * C) * Math.pow(A, 4)) / 24 +
					((61 - 58 * T + T * T + 600 * C - 330 * ep2) * Math.pow(A, 6)) / 720));
	return [E, Nn];
}

// flatten any geojson down to a flat list of lon/lat rings (for the area backdrop)
export function collectRings(gj: GeoJSON | null | undefined, out: Pt[][]): void {
	if (!gj) return;
	if ('features' in gj) {
		gj.features.forEach((feat) => collectRings(feat, out));
		return;
	}
	const g: Geom | null = 'geometry' in gj ? gj.geometry : gj;
	if (!g) return;
	const pushPoly = (poly: Pt[][]) => poly?.forEach((ring) => ring?.length && out.push(ring));
	if (g.type === 'Polygon') pushPoly(g.coordinates as Pt[][]);
	else if (g.type === 'MultiPolygon') (g.coordinates as Pt[][][]).forEach(pushPoly);
	else if (g.type === 'GeometryCollection') g.geometries?.forEach((gg) => collectRings(gg, out));
}

// drop near-collinear / dense points (tolerance in metres) so high-res boundaries stay
// cheap to redraw every frame
export function simplifyRing(pts: Pt[], tolM: number): Pt[] {
	if (pts.length < 3) return pts;
	const t2 = tolM * tolM;
	const out: Pt[] = [pts[0]];
	let last = pts[0];
	for (let i = 1; i < pts.length - 1; i++) {
		const dx = pts[i][0] - last[0],
			dy = pts[i][1] - last[1];
		if (dx * dx + dy * dy >= t2) {
			out.push(pts[i]);
			last = pts[i];
		}
	}
	out.push(pts[pts.length - 1]);
	return out;
}

// split a result geometry into polylines (segs) and standalone points (pts)
export function geomToParts(g: Geom | null, segs: Pt[][], pts: Pt[]): void {
	if (!g) return;
	const ls = (coords: Pt[]) => coords?.length && segs.push(coords.map((c) => [c[0], c[1]]));
	switch (g.type) {
		case 'Point':
			pts.push(g.coordinates as Pt);
			break;
		case 'MultiPoint':
			(g.coordinates as Pt[]).forEach((c) => pts.push(c));
			break;
		case 'LineString':
			ls(g.coordinates as Pt[]);
			break;
		case 'MultiLineString':
			(g.coordinates as Pt[][]).forEach(ls);
			break;
		case 'Polygon':
			(g.coordinates as Pt[][]).forEach(ls);
			break;
		case 'MultiPolygon':
			(g.coordinates as Pt[][][]).forEach((p) => p.forEach(ls));
			break;
		case 'GeometryCollection':
			g.geometries?.forEach((gg) => geomToParts(gg, segs, pts));
			break;
	}
}
