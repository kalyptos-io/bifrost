<script lang="ts">
	import { highlight, windowRange } from './json';

	let { lines }: { lines: string[] } = $props();

	let pane: HTMLPreElement;
	let viewH = $state(0);
	let scrollTop = $state(0);
	// measured once from css; never rounded, so offset and spacer share one float and cannot drift
	let rowH = $state(0);

	const maxLen = $derived(lines.reduce((m, l) => Math.max(m, l.length), 0));
	const win = $derived(windowRange(scrollTop, viewH, rowH, lines.length));
	const slice = $derived(highlight(lines.slice(win.start, win.end).join('\n')));

	$effect(() => {
		const cs = getComputedStyle(pane);
		const lh = parseFloat(cs.lineHeight);
		rowH = Number.isFinite(lh) ? lh : parseFloat(cs.fontSize) * 1.6;
	});

	$effect(() => {
		lines; // tracked, not used: a new response starts at the top
		scrollTop = 0;
		pane.scrollTop = 0;
	});
</script>

<pre
	class="json__b"
	bind:this={pane}
	bind:clientHeight={viewH}
	onscroll={() => (scrollTop = pane.scrollTop)}><div
		class="pad"
		style:height="{lines.length * rowH}px"
		style:min-width="{maxLen}ch"
	><code style:top="{win.start * rowH}px">{@html slice}</code></div></pre>

<style>
	.json__b {
		flex: 1;
		min-height: 0;
		margin: 0;
		padding: var(--space-2);
		overflow: auto;
		overscroll-behavior: contain;
		font-family: var(--font-mono);
		font-size: var(--size-mono-xs);
		line-height: 1.6;
		color: var(--text-inverse-secondary);
		tab-size: 2;
	}
	.pad {
		position: relative;
	}
	code {
		position: absolute;
		left: 0;
		font: inherit;
	}
	/* set by {@html}, so outside svelte's style scoping */
	.json__b :global(.k) {
		color: var(--pine-300);
	}
	.json__b :global(.s) {
		color: var(--bone-400);
	}
	.json__b :global(.n) {
		color: var(--moss-500);
	}
	.json__b :global(.l) {
		color: var(--graphite-400);
	}
</style>
