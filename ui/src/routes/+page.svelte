<script lang="ts">
	import Map from '$lib/Map.svelte';
	import JsonView from '$lib/JsonView.svelte';
	import {
		resolve,
		search,
		ApiError,
		RESOLVE_TARGETS,
		SEARCH_TARGETS,
		COMPONENT_FIELDS,
		LIFECYCLE_STATES,
		type ResolveTarget,
		type SearchTarget,
		type ComponentField,
		type Lifecycle,
		type Geometry
	} from '$lib/api';

	const version = import.meta.env.VITE_APP_VERSION ? `v${import.meta.env.VITE_APP_VERSION}` : 'dev';

	type Status = 'disabled' | 'searching' | 'error' | 'hit';

	const DEBOUNCE_MS = 250;

	const COL: Record<ComponentField, string> = {
		street: 'span 4',
		house_number: 'span 1',
		house_letter: 'span 1',
		floor: 'span 1',
		door: 'span 1',
		postcode: 'span 1',
		city: 'span 3',
		sub_locality: 'span 4'
	};
	const PH: Record<ComponentField, string> = {
		street: 'vejnavn',
		house_number: 'nr',
		house_letter: 'lit',
		floor: 'etage',
		door: 'dør',
		postcode: '1550',
		city: 'by',
		sub_locality: '-'
	};
	const LABEL: Record<ComponentField, string> = {
		street: 'street',
		house_number: 'house no',
		house_letter: 'letter',
		floor: 'floor',
		door: 'door',
		postcode: 'postcode',
		city: 'city',
		sub_locality: 'sub-locality'
	};

	let tab = $state<'resolve' | 'search'>('resolve');
	let resolveTarget = $state<ResolveTarget>('auto');
	let resOpen = $state(false);
	let freeform = $state('');
	let fields = $state<Record<ComponentField, string>>(
		Object.fromEntries(COMPONENT_FIELDS.map((k) => [k, ''])) as Record<ComponentField, string>
	);
	let searchQuery = $state('');
	let searchTarget = $state<SearchTarget>('street');
	let searchLimit = $state('5');
	let resolveLimit = $state('5');
	let regOpen = $state(false);
	let lifecycle = $state<Lifecycle[]>(['current']);
	let lcOpen = $state(false);
	let focus = $state<string | null>(null);
	let status = $state<Status>('disabled');
	let statusLine = $state('IDLE · enter an address to resolve');
	let match = $state<{
		result: string;
		kind: string;
		conf: string;
		score: string;
		lifecycle: string | null;
	} | null>(null);
	let matchCount = $state('0 MATCHES');
	let geometry = $state<Geometry | null>(null);
	let lines = $state<string[]>([]);
	let jsonOpen = $state(false);
	let optGeometry = $state(true);
	let optUuid = $state(false);

	let reqId = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;

	const hasInput = $derived(
		tab === 'search'
			? searchQuery.trim().length > 0
			: freeform.trim().length > 0 || COMPONENT_FIELDS.some((k) => fields[k].trim().length > 0)
	);

	const verb = $derived(tab === 'search' ? 'Search' : 'Resolve');
	const btn = $derived.by(() => {
		switch (status) {
			case 'disabled':
				return { hint: 'no input', cls: 'off' };
			case 'searching':
				return { hint: 'post', cls: 'busy' };
			default:
				return { hint: '↵', cls: 'go' };
		}
	});

	const mode = $derived.by(() => {
		switch (status) {
			case 'searching':
				return { t: tab === 'search' ? 'SEARCH' : 'RESOLVE', dot: 'stable' };
			case 'hit':
				return { t: 'HIT', dot: 'critical' };
			case 'error':
				return { t: 'ERROR', dot: 'dormant' };
			default:
				return { t: 'IDLE', dot: '' };
		}
	});

	function idleLine() {
		return (
			'IDLE · enter ' + (tab === 'search' ? 'a name to search a register' : 'an address to resolve')
		);
	}

	function clearResult() {
		geometry = null;
		match = null;
		matchCount = '0 MATCHES';
		lines = [];
	}

	function schedule() {
		clearTimeout(timer);
		if (!hasInput) {
			reqId++;
			clearResult();
			status = 'disabled';
			statusLine = idleLine();
			return;
		}
		timer = setTimeout(performSearch, DEBOUNCE_MS);
	}

	function switchTab(t: 'resolve' | 'search') {
		if (tab === t) return;
		reqId++;
		tab = t;
		status = 'disabled';
		clearResult();
		resOpen = false;
		regOpen = false;
		lcOpen = false;
		statusLine = idleLine();
		schedule();
	}

	async function performSearch() {
		clearTimeout(timer);
		if (!hasInput) {
			schedule();
			return;
		}
		const isSearch = tab === 'search';
		const id = ++reqId;
		status = 'searching';
		statusLine = isSearch ? 'SEARCHING · POST /search' : 'RESOLVING · POST /resolve';
		const qLabel = isSearch ? searchQuery.trim() : freeform.trim() || 'components';
		try {
			const data = isSearch
				? await search({
						query: searchQuery.trim(),
						target: searchTarget,
						limit: +searchLimit || 5,
						lifecycle,
						geometry: optGeometry
					})
				: await resolve({
						query: freeform.trim() || undefined,
						components: Object.fromEntries(
							COMPONENT_FIELDS.map((k) => [k, fields[k].trim()]).filter(([, v]) => v)
						),
						target: resolveTarget,
						lifecycle,
						geometry: optGeometry,
						uuid: optUuid,
						limit: +resolveLimit || 5
					});
			if (id !== reqId) return;
			lines = JSON.stringify(data, null, 2).split('\n');
			const matches = data.matches || [];
			if (!matches.length) {
				geometry = null;
				match = null;
				matchCount = '0 MATCHES';
				status = 'error';
				statusLine = 'NO MATCH · ' + qLabel;
				return;
			}
			const m = matches[0];
			const sc = m.meta?.score ?? null;
			const scoreTxt = sc == null ? '-' : sc.toFixed(2); // belief scores are signed
			const pcs = Array.isArray(m.postcodes) ? m.postcodes.filter(Boolean) : [];
			let resultText = m.result;
			if (pcs.length && !pcs.some((pc) => resultText.includes(pc)))
				resultText += ' · ' + pcs.join(', ');
			match = {
				result: resultText,
				kind: (m.kind || '-').toUpperCase(),
				conf: m.meta?.confidence ?? '-',
				score: scoreTxt,
				lifecycle: m.lifecycle ?? null
			};
			matchCount = matches.length + ' MATCH' + (matches.length > 1 ? 'ES' : '');
			status = 'hit';
			statusLine = 'HIT · ' + resultText;
			if (m.geometry?.geojson) geometry = m.geometry;
			else {
				geometry = null;
				statusLine = 'MATCH (no geometry) · ' + m.result;
			}
		} catch (err) {
			if (id !== reqId) return;
			clearResult();
			status = 'error';
			statusLine =
				err instanceof ApiError
					? err.message
					: 'CONNECTION BLOCKED · ' + (err instanceof Error ? err.message : String(err));
		}
	}

	function pickResolveTarget(t: ResolveTarget) {
		resolveTarget = t;
		resOpen = false;
		schedule();
	}
	function pickRegister(r: SearchTarget) {
		searchTarget = r;
		regOpen = false;
		schedule();
	}
	function toggleLifecycle(s: Lifecycle) {
		const on = lifecycle.includes(s);
		if (on && lifecycle.length === 1) return; // preserve one lifecycle
		lifecycle = LIFECYCLE_STATES.filter((x) => (x === s ? !on : lifecycle.includes(x)));
		schedule();
	}

	function onEnter(e: KeyboardEvent) {
		if (e.key === 'Enter') performSearch();
	}

	function clickOutside(node: HTMLElement, cb: () => void) {
		const handler = (e: PointerEvent) => {
			if (!node.contains(e.target as Node)) cb();
		};
		document.addEventListener('pointerdown', handler, true);
		return {
			destroy() {
				document.removeEventListener('pointerdown', handler, true);
			}
		};
	}
</script>

<svelte:head>
	<title>Bifrost · Address resolution</title>
</svelte:head>

{#snippet select(
	label: string,
	value: string,
	open: boolean,
	toggle: (v: boolean) => void,
	options: readonly string[],
	pick: (o: string) => void,
	sel: (o: string) => boolean
)}
	<div class="sel" use:clickOutside={() => toggle(false)}>
		<span class="lbl" class:hot={open}>{label}</span>
		<button class="trig" class:open onclick={() => toggle(!open)}>
			<span class="val">{value}</span>
			<svg class="car" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
		</button>
		{#if open}
			<div class="menu">
				{#each options as o (o)}
					<button class="opt" class:on={sel(o)} onclick={() => pick(o)}>
						<span class="mk">{sel(o) ? '↳' : ''}</span><span>{o}</span>
					</button>
				{/each}
			</div>
		{/if}
	</div>
{/snippet}

{#snippet toggle(label: string, on: boolean, set: (v: boolean) => void)}
	<label class="tog">
		<input
			type="checkbox"
			checked={on}
			onchange={(e) => {
				set(e.currentTarget.checked);
				schedule();
			}}
		/>
		<span>{label}</span>
	</label>
{/snippet}

{#snippet limitField(id: string, value: string, set: (v: string) => void)}
	<label class="fld" style:grid-column="span 1">
		<span class="lbl" class:hot={focus === id}>limit</span>
		<input
			class:on={focus === id}
			{value}
			oninput={(e) => {
				set(e.currentTarget.value.replace(/[^0-9]/g, ''));
				schedule();
			}}
			onkeydown={onEnter}
			onfocus={() => (focus = id)}
			onblur={() => (focus = null)}
			placeholder="5"
			spellcheck="false"
			autocomplete="off"
			inputmode="numeric"
		/>
	</label>
{/snippet}

{#snippet lifecycleSelect()}
	<section class="grp">
		<div class="k-eyebrow"><i></i>Lifecycle</div>
		{@render select(
			'include states',
			lifecycle.join(', '),
			lcOpen,
			(v: boolean) => (lcOpen = v),
			LIFECYCLE_STATES,
			(l) => toggleLifecycle(l as Lifecycle),
			(s: string) => lifecycle.includes(s as Lifecycle)
		)}
	</section>
{/snippet}

<div class="app">
	<section class="field">
		<Map {geometry} />

		<div class="ident">
			<div class="wordmark">Bifrost</div>
			<div class="tagline">{version}</div>
		</div>

		{#if match}
			<div class="plate">
				<div class="k-eyebrow k-eyebrow--inverse"><i></i>Match 01</div>
				<div class="plate__t">{match.result}</div>
				<dl class="plate__specs">
					<div class="k-data k-data--inverse">
						<span class="k-data__k">Kind</span><i class="k-data__lead"></i><span class="k-data__v"
							>{match.kind}</span
						>
					</div>
					<div class="k-data k-data--inverse">
						<span class="k-data__k">Score</span><i class="k-data__lead"></i><span class="k-data__v"
							>{match.score}</span
						>
					</div>
					<div class="k-data k-data--inverse">
						<span class="k-data__k">Confidence</span><i class="k-data__lead"></i><span
							class="k-data__v">{match.conf}</span
						>
					</div>
					{#if match.lifecycle}
						<div class="k-data k-data--inverse">
							<span class="k-data__k">Lifecycle</span><i class="k-data__lead"></i><span
								class="k-data__v">{match.lifecycle}</span
							>
						</div>
					{/if}
				</dl>
			</div>
		{/if}

		{#if lines.length}
			<div class="json" class:open={jsonOpen}>
				<button class="json__h" onclick={() => (jsonOpen = !jsonOpen)} aria-expanded={jsonOpen}
					>JSON</button
				>
				<div class="json__p">
					<div class="json__opts">
						{@render toggle('geometry', optGeometry, (v) => (optGeometry = v))}
						{#if tab === 'resolve'}
							{@render toggle('uuid', optUuid, (v) => (optUuid = v))}
						{/if}
					</div>
					<JsonView {lines} />
				</div>
			</div>
		{/if}
	</section>

	<div class="sheet">
		<div class="sheet__body">
			<nav class="pnav">
				<button aria-current={tab === 'resolve' ? 'page' : undefined} onclick={() => switchTab('resolve')}
					>01 Resolve</button
				>
				<button aria-current={tab === 'search' ? 'page' : undefined} onclick={() => switchTab('search')}
					>02 Search</button
				>
			</nav>

			<div class="doc">
				{#if tab === 'resolve'}
					<section class="grp">
						<div class="k-eyebrow"><i></i>Target</div>
						<div class="grid">
							<div style:grid-column="span 3">
								{@render select(
									'project to',
									resolveTarget,
									resOpen,
									(v: boolean) => (resOpen = v),
									RESOLVE_TARGETS,
									(t) => pickResolveTarget(t as ResolveTarget),
									(t: string) => t === resolveTarget
								)}
							</div>
							{@render limitField('__rl', resolveLimit, (v) => (resolveLimit = v))}
						</div>
					</section>

					<section class="grp">
						<div class="k-eyebrow k-eyebrow--accent"><i></i>Query</div>
						<div class="q" class:on={focus === '__ff'}>
							<span class="q__m">↳</span>
							<input
								value={freeform}
								oninput={(e) => {
									freeform = e.currentTarget.value;
									schedule();
								}}
								onkeydown={onEnter}
								onfocus={() => (focus = '__ff')}
								onblur={() => (focus = null)}
								placeholder="free-form address"
								spellcheck="false"
								autocomplete="off"
							/>
						</div>
					</section>

					<section class="grp">
						<div class="k-eyebrow"><i></i>Components</div>
						<div class="grid">
							{#each COMPONENT_FIELDS as k (k)}
								<label class="fld" style:grid-column={COL[k]}>
									<span class="lbl" class:hot={focus === k}>{LABEL[k]}</span>
									<input
										class:on={focus === k}
										value={fields[k]}
										oninput={(e) => {
											fields[k] = e.currentTarget.value;
											schedule();
										}}
										onkeydown={onEnter}
										onfocus={() => (focus = k)}
										onblur={() => (focus = null)}
										placeholder={PH[k]}
										spellcheck="false"
										autocomplete="off"
									/>
								</label>
							{/each}
						</div>
					</section>

					{@render lifecycleSelect()}
				{:else}
					<section class="grp">
						<div class="k-eyebrow"><i></i>Register</div>
						<div class="grid">
							<div style:grid-column="span 3">
								{@render select(
									'target register',
									searchTarget,
									regOpen,
									(v: boolean) => (regOpen = v),
									SEARCH_TARGETS,
									(r) => pickRegister(r as SearchTarget),
									(r: string) => r === searchTarget
								)}
							</div>
							{@render limitField('__sl', searchLimit, (v) => (searchLimit = v))}
						</div>
					</section>

					<section class="grp">
						<div class="k-eyebrow k-eyebrow--accent"><i></i>Query</div>
						<div class="q" class:on={focus === '__sq'}>
							<span class="q__m">↳</span>
							<input
								value={searchQuery}
								oninput={(e) => {
									searchQuery = e.currentTarget.value;
									schedule();
								}}
								onkeydown={onEnter}
								onfocus={() => (focus = '__sq')}
								onblur={() => (focus = null)}
								placeholder="name to look up in register"
								spellcheck="false"
								autocomplete="off"
							/>
						</div>
					</section>

					{@render lifecycleSelect()}
				{/if}

				<button
					class="act {btn.cls}"
					disabled={status === 'disabled' || status === 'searching'}
					onclick={performSearch}
				>
					<span>{verb}</span>
					{#if btn.cls === 'go'}
						<svg class="act__i" viewBox="0 0 24 24" aria-hidden="true"
							><path d="M20 4v7a4 4 0 0 1-4 4H4" /><path d="m9 10-5 5 5 5" /></svg
						>
					{:else}
						<span class="act__h">{btn.hint}</span>
					{/if}
				</button>
			</div>
		</div>

		<div class="sheet__foot">
			<div class="status">
				<i class="k-dot {mode.dot ? 'k-dot--' + mode.dot : ''}"></i>{mode.t}
			</div>
			<div class="line">{statusLine}</div>
			<div class="cnt">{matchCount}</div>
		</div>
	</div>
</div>

<style>
	.app {
		position: fixed;
		inset: 0;
		display: flex;
		background: var(--bone-100);
		color: var(--text-primary);
		font-family: var(--font-text);
		font-size: var(--size-body);
		line-height: var(--lh-body);
	}

	/* map field */
	.field {
		--inset: var(--space-4);
		position: relative;
		flex: 1;
		min-width: 0;
		overflow: hidden;
		background: var(--ink-950);
		color: var(--text-inverse);
	}
	.ident {
		position: absolute;
		left: var(--inset);
		/* align cap height to inset */
		top: calc(var(--inset) - 8px);
		pointer-events: none;
	}
	.tagline {
		margin-top: 10px;
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: var(--graphite-400);
	}
	.plate {
		position: absolute;
		left: var(--inset);
		bottom: calc(var(--inset) + var(--space-4));
		width: min(420px, calc(100% - var(--inset) * 2));
		display: flex;
		flex-direction: column;
		gap: 14px;
		pointer-events: none;
	}
	.plate__t {
		font-family: var(--font-sans);
		font-weight: var(--weight-medium);
		font-size: var(--size-title-2);
		line-height: var(--lh-title-2);
		letter-spacing: var(--tracking-title-2);
		text-wrap: pretty;
	}
	.plate__specs {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 0;
		padding-top: 14px;
		border-top: 1px solid var(--line-inverse);
	}

	/* json pane */
	.json {
		--w: 340px;
		position: absolute;
		top: var(--inset);
		bottom: var(--inset);
		right: 0;
		display: flex;
		align-items: flex-start;
		max-width: calc(100% - var(--inset));
		transform: translateX(var(--w));
		transition: transform var(--dur-base) var(--ease-veil);
	}
	.json.open {
		transform: translateX(0);
	}
	.json__h {
		flex: none;
		padding: 14px 7px;
		background: var(--ink-900);
		border: 1px solid var(--line-inverse);
		border-right: 0;
		writing-mode: vertical-rl;
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		letter-spacing: 0.22em;
		color: var(--text-inverse-secondary);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.json__h:hover,
	.json.open .json__h {
		color: var(--text-inverse);
	}
	.json__p {
		flex: none;
		display: flex;
		flex-direction: column;
		width: var(--w);
		max-width: 100%;
		height: 100%;
		border: 1px solid var(--line-inverse);
		background: var(--ink-900);
	}
	.json__opts {
		flex: none;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		padding: 9px var(--space-2);
		border-bottom: 1px solid var(--line-inverse);
	}
	.tog {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: var(--graphite-400);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.tog:has(input:checked) {
		color: var(--text-inverse);
	}
	.tog input {
		appearance: none;
		flex: none;
		position: relative;
		width: 22px;
		height: 12px;
		margin: 0;
		background: none;
		border: 1px solid var(--line-inverse-strong);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.tog input::after {
		content: '';
		position: absolute;
		top: 1px;
		left: 1px;
		width: 8px;
		height: 8px;
		background: var(--graphite-400);
		transition: transform var(--dur-fast) var(--ease-veil), background var(--dur-fast) var(--ease-veil);
	}
	.tog input:checked {
		border-color: var(--pine-400);
	}
	.tog input:checked::after {
		background: var(--pine-400);
		transform: translateX(10px);
	}

	/* controls */
	.sheet {
		--doc: 460px;
		/* sized by the form, not by a share of the viewport: no dead space at wide widths */
		flex: none;
		width: calc(var(--doc) + var(--space-3) * 2);
		display: flex;
		flex-direction: column;
		border-left: 1px solid var(--line-hairline);
	}
	.sheet__body {
		flex: 1;
		overflow-y: auto;
		padding: var(--space-3) var(--space-3) var(--space-4);
	}
	.doc {
		display: flex;
		flex-direction: column;
		gap: 40px;
		max-width: var(--doc);
	}

	.pnav {
		display: flex;
		column-gap: 22px;
		margin-bottom: var(--space-5);
		font-family: var(--font-mono);
		font-size: var(--size-mono-sm);
		letter-spacing: 0.22em;
		text-transform: uppercase;
	}
	.pnav button {
		padding: 0;
		background: none;
		border: 0;
		font: inherit;
		letter-spacing: inherit;
		text-transform: inherit;
		cursor: pointer;
		white-space: nowrap;
		color: var(--graphite-400);
		transition: var(--transition-control);
	}
	.pnav button:hover {
		color: var(--text-primary);
	}
	.pnav button[aria-current='page'] {
		color: var(--pine-500);
	}

	.grp {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}

	.lbl {
		font-family: var(--font-mono);
		font-size: var(--size-mono-sm);
		letter-spacing: 0.18em;
		text-transform: uppercase;
		color: var(--text-tertiary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		transition: var(--transition-control);
	}
	.lbl.hot {
		color: var(--text-secondary);
	}

	.sel {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
	}
	.trig {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-1);
		padding: 0 0 6px;
		background: none;
		border: 0;
		border-bottom: 1px solid var(--line-strong);
		font: inherit;
		font-size: var(--size-body-sm);
		text-align: left;
		color: var(--text-primary);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.trig:hover,
	.trig.open {
		border-bottom-color: var(--line-accent);
	}
	.val {
		flex: 1;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.car {
		flex: none;
		width: 14px;
		height: 14px;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.75;
		color: var(--text-tertiary);
		transition: transform var(--dur-fast) var(--ease-veil), color var(--dur-fast) var(--ease-veil);
	}
	.trig.open .car {
		transform: rotate(180deg);
		color: var(--text-accent);
	}
	.menu {
		position: absolute;
		left: 0;
		right: 0;
		top: 100%;
		z-index: 20;
		max-height: 244px;
		overflow-y: auto;
		background: var(--bone-050);
		border: var(--border-hairline);
		box-shadow: var(--shadow-overlay);
	}
	.opt {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		width: 100%;
		padding: 7px 14px;
		background: none;
		border: 0;
		border-bottom: var(--border-quiet);
		font-family: var(--font-mono);
		font-size: var(--size-mono-sm);
		letter-spacing: 0.06em;
		text-transform: uppercase;
		text-align: left;
		color: var(--graphite-400);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.opt:last-child {
		border-bottom: 0;
	}
	.opt:hover,
	.opt.on {
		color: var(--text-accent);
	}
	.mk {
		flex: none;
		width: 10px;
		color: var(--line-accent);
	}

	.q {
		display: flex;
		align-items: baseline;
		gap: 10px;
		padding-bottom: 8px;
		border-bottom: 1px solid var(--line-strong);
		transition: var(--transition-control);
	}
	.q.on {
		border-bottom-color: var(--line-accent);
	}
	.q__m {
		flex: none;
		font-family: var(--font-mono);
		font-size: var(--size-mono-sm);
		color: var(--text-accent);
	}
	.q.on .q__m {
		color: var(--text-accent);
	}
	.q input {
		flex: 1;
		min-width: 0;
		padding: 0;
		background: none;
		border: 0;
		outline: none;
		font: inherit;
		font-size: var(--size-body-sm);
		color: var(--text-primary);
		caret-color: var(--pine-500);
	}

	.grid {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 20px 14px;
	}
	.fld {
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
	}
	.fld input {
		min-width: 0;
		padding: 0 0 6px;
		background: none;
		border: 0;
		border-bottom: 1px solid var(--line-strong);
		outline: none;
		font: inherit;
		font-size: var(--size-body-sm);
		color: var(--text-primary);
		caret-color: var(--pine-500);
		transition: var(--transition-control);
	}
	.fld input:hover,
	.fld input.on {
		border-bottom-color: var(--line-accent);
	}

	.act {
		display: inline-flex;
		align-items: center;
		justify-content: space-between;
		align-self: flex-start;
		gap: var(--space-3);
		min-width: 190px;
		height: 38px;
		padding: 0 var(--space-3);
		background: none;
		border: 1px solid var(--line-strong);
		font-family: var(--font-text);
		font-weight: var(--weight-medium);
		font-size: 11.5px;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: var(--text-primary);
		cursor: pointer;
		transition: var(--transition-control);
	}
	.act__h {
		font-family: var(--font-mono);
		font-size: var(--size-mono-sm);
		letter-spacing: 0.14em;
		color: var(--text-tertiary);
	}
	.act__i {
		width: 14px;
		height: 14px;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.75;
	}
	.act.off {
		color: var(--graphite-300);
		border-color: var(--line-hairline);
		cursor: not-allowed;
	}
	.act.off .act__h {
		color: var(--graphite-300);
	}
	.act.busy {
		cursor: default;
	}
	.act.go {
		background: var(--pine-500);
		border-color: var(--pine-500);
		color: var(--bone-100);
	}
	.act.go .act__h {
		color: var(--bone-400);
	}
	.act.go .act__i {
		color: var(--bone-400);
	}
	.act.go:hover {
		background: var(--pine-400);
		border-color: var(--pine-400);
	}
	.act:focus-visible {
		outline: none;
		box-shadow: var(--shadow-focus);
	}

	.sheet__foot {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: 0 var(--space-3);
		height: 42px;
		border-top: var(--border-hairline);
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		letter-spacing: 0.16em;
		text-transform: uppercase;
	}
	.sheet__foot .status {
		flex: none;
		font-size: inherit;
		letter-spacing: inherit;
		color: var(--text-secondary);
	}
	.line {
		flex: 1;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		text-transform: none;
		letter-spacing: 0.06em;
		color: var(--text-tertiary);
	}
	.cnt {
		flex: none;
		color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
	}

	:global(button:focus-visible),
	:global(input:focus-visible) {
		outline: none;
		box-shadow: var(--shadow-focus);
	}

	/* stack only once the side-by-side map gets too narrow; the stacked form then fills the width */
	@media (max-width: 900px) {
		.app {
			position: static;
			flex-direction: column;
		}
		.field {
			flex: none;
			height: max(420px, 60svh);
		}
		.sheet {
			width: auto;
			border-left: 0;
			border-top: 1px solid var(--line-hairline);
		}
		.doc {
			max-width: none;
		}
		.sheet__body {
			padding: var(--space-3) var(--m) var(--space-4);
		}
		.sheet__foot {
			padding: 0 var(--m);
		}
	}
</style>
