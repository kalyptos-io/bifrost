<script lang="ts">
	import '@fontsource-variable/archivo';
	import '@fontsource/ibm-plex-mono/latin-400.css';
	import '@fontsource/ibm-plex-mono/latin-500.css';
	import '@fontsource/syne/latin-500.css';
	import '@fontsource/syne/latin-600.css';
	import '$lib/styles/kalyptos.css';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

{@render children()}

<style>
	/* map reads these variables through getcomputedstyle */
	:global(:root) {
		--map-bg: var(--ink-950);
		--map-grid: rgba(246, 242, 234, 0.032);
		--map-dot: rgba(246, 242, 234, 0.07);
		--map-land: var(--ink-900);
		--map-coast: rgba(246, 242, 234, 0.22);
		--map-result: var(--pine-300);
		/* bare channels are interpolated into rgba values */
		--map-accent-rgb: 23, 160, 109;
	}

	:global(html),
	:global(body) {
		margin: 0;
		height: 100%;
		background: var(--bone-100);
		overflow: hidden;
	}
	:global(*) {
		box-sizing: border-box;
	}
	:global(::selection) {
		background: rgba(10, 113, 80, 0.18);
	}
	:global(input::placeholder) {
		color: var(--graphite-300);
	}
	:global(::-webkit-scrollbar) {
		width: 8px;
		height: 8px;
	}
	:global(::-webkit-scrollbar-thumb) {
		background: var(--line-hairline);
	}
	:global(::-webkit-scrollbar-track) {
		background: transparent;
	}
	@media (max-width: 1100px) {
		:global(body) {
			overflow-y: auto;
		}
	}
</style>
