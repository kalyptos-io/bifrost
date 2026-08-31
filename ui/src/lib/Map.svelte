<script lang="ts">
	import { onMount } from 'svelte';
	import { lonLatToUTM, collectRings, simplifyRing, geomToParts, type Pt, type GeoJSON } from './geo';
	import type { Geometry } from './api';

	let { geometry = null }: { geometry: Geometry | null } = $props();

	let canvas: HTMLCanvasElement;
	let zEl: HTMLSpanElement, curEl: HTMLSpanElement;

	// imperative, non-reactive render state (mutated per-frame, read by draw)
	type View = { cx: number; cy: number; scale: number };
	type ResultGeom = {
		src: Pt[][];
		segs: Pt[][];
		simpScale: number;
		pts: Pt[];
		total: number;
		isPoly: boolean;
		marker: Pt;
		t0: number;
		dur: number;
	};
	let ctx: CanvasRenderingContext2D | null = null;
	let size = { w: 0, h: 0, dpr: 1 };
	let view: View = { cx: 580000, cy: 6225000, scale: 0.0008 };
	let outline: Pt[][] | null = null;
	let dkBBox: [number, number, number, number] = [0, 0, 0, 0];
	let geom: ResultGeom | null = null;
	let animFrom: View | null = null,
		animTo: View | null = null,
		animStart = 0,
		animDur = 0;
	let drag: { x: number; y: number } | null = null;
	let raf = 0;
	let blink = true;
	let cw = $state(0),
		ch = $state(0);
	// one static palette, no theme switching: read the vars once instead of per frame
	const MAP_VARS = [
		'--map-bg',
		'--map-grid',
		'--map-dot',
		'--map-land',
		'--map-coast',
		'--map-result',
		'--map-accent-rgb'
	];
	let col: Record<string, string> = {};

	function buildOutline(gj: GeoJSON | null): boolean {
		if (!gj) return false;
		const rings: Pt[][] = [];
		collectRings(gj, rings);
		if (!rings.length) return false;
		let minE = 1e15,
			minN = 1e15,
			maxE = -1e15,
			maxN = -1e15;
		outline = rings.map((ring) =>
			simplifyRing(
				ring.map(([lon, lat]) => {
					const p = lonLatToUTM(lon, lat);
					if (p[0] < minE) minE = p[0];
					if (p[0] > maxE) maxE = p[0];
					if (p[1] < minN) minN = p[1];
					if (p[1] > maxN) maxN = p[1];
					return p;
				}),
				40
			)
		);
		dkBBox = [minE, minN, maxE, maxN];
		return true;
	}

	function resize(w: number, h: number) {
		const dpr = Math.min(window.devicePixelRatio || 1, 2);
		size = { w, h, dpr };
		canvas.width = Math.round(w * dpr);
		canvas.height = Math.round(h * dpr);
	}

	// coalesce every mutation in a frame into one draw; the loop runs only while something animates
	function requestDraw() {
		if (!raf) raf = requestAnimationFrame(frame);
	}
	function frame(now: number) {
		raf = 0;
		draw(now);
		if (animTo || (geom && now - geom.t0 < geom.dur)) requestDraw();
	}

	function minScale() {
		const { w, h } = size;
		const [a, b, c, d] = dkBBox;
		return 0.55 * Math.min(w / (c - a), h / (d - b));
	}
	const maxScale = () => 0.6;
	const clampScale = (s: number) => Math.max(minScale(), Math.min(maxScale(), s));

	function fitBBox(bbox: [number, number, number, number], padFrac: number, animate: boolean) {
		let [minE, minN, maxE, maxN] = bbox;
		let dx = maxE - minE,
			dy = maxN - minN;
		const minSpan = 14000; // metres - stay zoomed out enough to keep coastline in frame
		if (dx < minSpan) {
			const c = (minE + maxE) / 2;
			minE = c - minSpan / 2;
			maxE = c + minSpan / 2;
			dx = minSpan;
		}
		if (dy < minSpan) {
			const c = (minN + maxN) / 2;
			minN = c - minSpan / 2;
			maxN = c + minSpan / 2;
			dy = minSpan;
		}
		const { w, h } = size;
		const pad = 1 - (padFrac || 0.15);
		let scale = Math.min(w / dx, h / dy) * pad;
		scale = Math.max(minScale(), Math.min(maxScale(), scale));
		const to: View = { cx: (minE + maxE) / 2, cy: (minN + maxN) / 2, scale };
		if (animate) animateTo(to, 950);
		else {
			view = { ...to };
			animTo = null;
		}
		requestDraw();
	}
	function animateTo(to: View, dur: number) {
		animFrom = { ...view };
		animTo = { ...to };
		animStart = performance.now();
		animDur = dur;
	}

	function setGeometry(g: Geometry) {
		const src: Pt[][] = [],
			pts: Pt[] = [];
		geomToParts(g.geojson, src, pts);
		const isPoly = g.geojson.type === 'Polygon' || g.geojson.type === 'MultiPolygon';
		let minE = 1e15,
			minN = 1e15,
			maxE = -1e15,
			maxN = -1e15;
		const acc = (x: number, y: number) => {
			if (x < minE) minE = x;
			if (x > maxE) maxE = x;
			if (y < minN) minN = y;
			if (y > maxN) maxN = y;
		};
		src.forEach((s) => s.forEach(([x, y]) => acc(x, y)));
		pts.forEach(([x, y]) => acc(x, y));
		const vp = g.vejpunkt || null;
		if (vp) acc(vp[0], vp[1]);
		if (minE > maxE) return;
		const marker: Pt = vp || pts[0] || [(minE + maxE) / 2, (minN + maxN) / 2];
		// simpScale 0 makes the first draw decimate at whatever scale the fit lands on
		geom = {
			src,
			segs: [],
			simpScale: 0,
			pts,
			total: 0,
			isPoly,
			marker,
			t0: performance.now(),
			dur: 1100
		};
		fitBBox([minE, minN, maxE, maxN], 0.28, true);
	}

	// decimate to half a pixel at the current zoom; re-run only past a 1.5x scale move
	function resimplify(G: ResultGeom, scale: number) {
		G.segs = G.src.map((seg) => simplifyRing(seg, 0.5 / scale));
		G.simpScale = scale;
		let total = 0;
		for (const seg of G.segs)
			for (let i = 1; i < seg.length; i++)
				total += Math.hypot(seg[i][0] - seg[i - 1][0], seg[i][1] - seg[i - 1][1]);
		G.total = total;
	}

	function onPointerDown(e: PointerEvent) {
		drag = { x: e.clientX, y: e.clientY };
		animTo = null;
		try {
			canvas.setPointerCapture(e.pointerId);
		} catch {
			/* no capture */
		}
		canvas.style.cursor = 'grabbing';
	}
	function onPointerMove(e: PointerEvent) {
		if (!size.w) return;
		const r = canvas.getBoundingClientRect();
		const mx = e.clientX - r.left,
			my = e.clientY - r.top;
		const { w, h } = size;
		const E = view.cx + (mx - w / 2) / view.scale,
			N = view.cy - (my - h / 2) / view.scale;
		if (curEl)
			curEl.textContent =
				Math.round(E).toLocaleString('en') + ' E  ' + Math.round(N).toLocaleString('en') + ' N';
		if (drag) {
			const dx = e.clientX - drag.x,
				dy = e.clientY - drag.y;
			drag = { x: e.clientX, y: e.clientY };
			view.cx -= dx / view.scale;
			view.cy += dy / view.scale;
			animTo = null;
			requestDraw();
		}
	}
	function onPointerUp() {
		drag = null;
		canvas.style.cursor = 'grab';
	}
	function onWheel(e: WheelEvent) {
		e.preventDefault();
		if (!size.w) return;
		const r = canvas.getBoundingClientRect();
		const mx = e.clientX - r.left,
			my = e.clientY - r.top;
		const { w, h } = size;
		const wx = view.cx + (mx - w / 2) / view.scale,
			wy = view.cy - (my - h / 2) / view.scale;
		const ns = clampScale(view.scale * Math.exp(-e.deltaY * 0.0016));
		view.cx = wx - (mx - w / 2) / ns;
		view.cy = wy + (my - h / 2) / ns;
		view.scale = ns;
		animTo = null;
		requestDraw();
	}

	function niceStep(targetPx: number) {
		const metres = targetPx / view.scale;
		const pow = Math.pow(10, Math.floor(Math.log10(metres)));
		const cands = [1, 2, 5, 10].map((m) => m * pow);
		let best = cands[0];
		for (const c of cands) if (Math.abs(c - metres) < Math.abs(best - metres)) best = c;
		return best;
	}

	function draw(now: number) {
		if (!ctx || !size.w) return;
		const { w, h, dpr } = size;
		const v = view;
		const c = (n: string) => col[n];
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, w, h);
		ctx.fillStyle = c('--map-bg');
		ctx.fillRect(0, 0, w, h);
		const P = (E: number, N: number): [number, number] => [
			w / 2 + (E - v.cx) * v.scale,
			h / 2 - (N - v.cy) * v.scale
		];

		// grid
		const step = niceStep(110);
		const left = v.cx - w / 2 / v.scale,
			right = v.cx + w / 2 / v.scale;
		const bot = v.cy - h / 2 / v.scale,
			top = v.cy + h / 2 / v.scale;
		ctx.lineWidth = 1;
		ctx.strokeStyle = c('--map-grid');
		ctx.beginPath();
		for (let E = Math.ceil(left / step) * step; E <= right; E += step) {
			const x = Math.round(P(E, 0)[0]) + 0.5;
			ctx.moveTo(x, 0);
			ctx.lineTo(x, h);
		}
		for (let N = Math.ceil(bot / step) * step; N <= top; N += step) {
			const y = Math.round(P(0, N)[1]) + 0.5;
			ctx.moveTo(0, y);
			ctx.lineTo(w, y);
		}
		ctx.stroke();
		ctx.fillStyle = c('--map-dot');
		for (let E = Math.ceil(left / step) * step; E <= right; E += step)
			for (let N = Math.ceil(bot / step) * step; N <= top; N += step) {
				const [x, y] = P(E, N);
				ctx.fillRect(x - 0.5, y - 0.5, 1.5, 1.5);
			}

		// denmark outline
		if (outline) {
			ctx.lineJoin = 'round';
			ctx.lineCap = 'round';
			ctx.beginPath();
			for (const ring of outline) {
				for (let i = 0; i < ring.length; i++) {
					const [x, y] = P(ring[i][0], ring[i][1]);
					i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
				}
				ctx.closePath();
			}
			ctx.fillStyle = c('--map-land');
			ctx.fill();
			ctx.lineWidth = 1;
			ctx.strokeStyle = c('--map-coast');
			ctx.stroke();
		}

		// animation tween
		if (animTo && animFrom) {
			const t = Math.min(1, (now - animStart) / animDur),
				e = 1 - Math.pow(1 - t, 3);
			const A = animFrom,
				B = animTo;
			view = {
				cx: A.cx + (B.cx - A.cx) * e,
				cy: A.cy + (B.cy - A.cy) * e,
				scale: Math.exp(Math.log(A.scale) + (Math.log(B.scale) - Math.log(A.scale)) * e)
			};
			if (t >= 1) animTo = null;
		}

		// result geometry
		if (geom) {
			const G = geom;
			const r = v.scale / G.simpScale;
			if (r > 1.5 || r < 1 / 1.5) resimplify(G, v.scale);
			const p = Math.min(1, (now - G.t0) / G.dur),
				e = 1 - Math.pow(1 - p, 3);
			const budget = G.total * e;
			if (G.isPoly) {
				ctx.beginPath();
				for (const s of G.segs) {
					for (let i = 0; i < s.length; i++) {
						const [x, y] = P(s[i][0], s[i][1]);
						i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
					}
					ctx.closePath();
				}
				ctx.fillStyle = 'rgba(' + c('--map-accent-rgb') + ',' + (0.1 * e).toFixed(3) + ')';
				ctx.fill();
			}
			ctx.lineWidth = 2;
			ctx.strokeStyle = c('--map-result');
			ctx.lineJoin = 'round';
			ctx.lineCap = 'round';
			let rem = budget;
			ctx.beginPath();
			for (const s of G.segs) {
				if (G.total > 0 && rem <= 0 && e < 1) break;
				const [x0, y0] = P(s[0][0], s[0][1]);
				ctx.moveTo(x0, y0);
				for (let i = 1; i < s.length; i++) {
					const segd = e >= 1 ? 0 : Math.hypot(s[i][0] - s[i - 1][0], s[i][1] - s[i - 1][1]);
					if (e >= 1 || rem >= segd) {
						const [x, y] = P(s[i][0], s[i][1]);
						ctx.lineTo(x, y);
						rem -= segd;
					} else {
						const f = rem / segd;
						const ix = s[i - 1][0] + (s[i][0] - s[i - 1][0]) * f,
							iy = s[i - 1][1] + (s[i][1] - s[i - 1][1]) * f;
						const [x, y] = P(ix, iy);
						ctx.lineTo(x, y);
						rem = 0;
						break;
					}
				}
			}
			ctx.stroke();
			if (e > 0.25)
				for (const pt of G.pts) {
					const [x, y] = P(pt[0], pt[1]);
					ctx.fillStyle = c('--map-result');
					ctx.fillRect(x - 2, y - 2, 4, 4);
				}
			// blinking marker reticle
			const [mx, my] = P(G.marker[0], G.marker[1]);
			ctx.strokeStyle = 'rgba(' + c('--map-accent-rgb') + ',0.28)';
			ctx.lineWidth = 1;
			ctx.beginPath();
			ctx.moveTo(0, Math.round(my) + 0.5);
			ctx.lineTo(w, Math.round(my) + 0.5);
			ctx.moveTo(Math.round(mx) + 0.5, 0);
			ctx.lineTo(Math.round(mx) + 0.5, h);
			ctx.stroke();
			const rr = 9;
			ctx.strokeStyle = c('--map-result');
			ctx.lineWidth = 1.5;
			ctx.strokeRect(mx - rr, my - rr, 2 * rr, 2 * rr);
			if (blink) {
				ctx.fillStyle = c('--map-result');
				ctx.fillRect(mx - 2.5, my - 2.5, 5, 5);
			}
		}

		// readouts
		if (zEl) {
			const mpp = 1 / v.scale;
			zEl.textContent = mpp >= 1000 ? (mpp / 1000).toFixed(1) + ' km/px' : Math.round(mpp) + ' m/px';
		}
	}

	onMount(() => {
		ctx = canvas.getContext('2d');
		// the size bindings only land on the first observer tick, too late for the backdrop fit
		resize(canvas.clientWidth, canvas.clientHeight);
		const cs = getComputedStyle(canvas);
		for (const n of MAP_VARS) col[n] = cs.getPropertyValue(n).trim();
		// bundled backdrop; edges share the source the API's polygons are authored from
		fetch('/denmark.geojson')
			.then((r) => r.json())
			.then((gj) => {
				if (buildOutline(gj)) {
					if (!geom) fitBBox(dkBBox, 0.12, false);
					requestDraw();
				}
			})
			.catch(() => {});
		return () => cancelAnimationFrame(raf);
	});

	$effect(() => {
		if (cw && ch) {
			resize(cw, ch);
			requestDraw();
		}
	});

	// the marker reticle is the only perpetual animation; ticking it beats holding a 60fps loop open
	$effect(() => {
		if (!geometry) return;
		const id = setInterval(() => {
			if (!geom) return;
			blink = !blink;
			requestDraw();
		}, 480);
		return () => clearInterval(id);
	});

	// imperative canvas reaction to a new result geometry (the prefer-derived exception)
	$effect(() => {
		if (geometry) setGeometry(geometry);
		else geom = null;
		requestDraw();
	});
</script>

<svelte:window onpointerup={onPointerUp} />

<div class="map">
	<canvas
		bind:this={canvas}
		bind:clientWidth={cw}
		bind:clientHeight={ch}
		onpointerdown={onPointerDown}
		onpointermove={onPointerMove}
		onwheel={onWheel}
	></canvas>

	<div class="foot">
		<span><span class="k">Cursor</span> <span class="v" bind:this={curEl}>-</span></span>
		<span><span class="k">Scale</span> <span class="v" bind:this={zEl}>-</span></span>
	</div>
</div>

<style>
	.map {
		position: absolute;
		inset: 0;
		background: var(--map-bg);
		overflow: hidden;
	}
	canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
		cursor: grab;
	}
	.foot {
		position: absolute;
		/* --inset is the field's margin, set on .field; --m is the page one and too wide here */
		left: var(--inset);
		right: var(--inset);
		/* 4px below the inset so the baseline, not the line box, sits on the margin */
		bottom: calc(var(--inset) - 4px);
		display: flex;
		flex-wrap: wrap;
		gap: 4px 32px;
		pointer-events: none;
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		letter-spacing: 0.16em;
		text-transform: uppercase;
		font-variant-numeric: tabular-nums;
	}
	.k {
		color: var(--graphite-500);
	}
	.v {
		color: var(--graphite-300);
	}
</style>
