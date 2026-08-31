import mdx from '@astrojs/mdx';
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';

// base is baked into every asset url at build time; changing it means rebuilding the image
const base = process.env.DOCS_BASE || '/docs';
const prefix = base.replace(/\/$/, '');

// examples are written against this host; the runtime script below swaps it for the deployed base
const EXAMPLE_HOST = 'https://bifrost.kalyptos.io';

const rewrite = `addEventListener('DOMContentLoaded',function(){
var h=${JSON.stringify(EXAMPLE_HOST)},b=window.__BIFROST_API__||location.origin;
if(b===h)return;
var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),n;
while((n=w.nextNode()))if(n.nodeValue.indexOf(h)>=0)n.nodeValue=n.nodeValue.split(h).join(b);
document.querySelectorAll('a[href^="'+h+'"]').forEach(function(a){a.href=b+a.getAttribute('href').slice(h.length)});
document.querySelectorAll('[data-code]').forEach(function(e){e.dataset.code=e.dataset.code.split(h).join(b)});
});`;

export default defineConfig({
	base,
	site: EXAMPLE_HOST,
	integrations: [
		starlight({
			title: 'Bifrost API',
			description: 'Danish address resolution and register lookup over two JSON endpoints.',
			favicon: '/favicon.svg',
			head: [
				{ tag: 'script', attrs: { src: `${prefix}/env.js` } },
				{ tag: 'script', content: rewrite }
			],
			customCss: [
				'@fontsource-variable/archivo',
				'@fontsource/ibm-plex-mono/latin-400.css',
				'@fontsource/ibm-plex-mono/latin-500.css',
				'@fontsource/syne/latin-500.css',
				'@fontsource/syne/latin-600.css',
				'./src/styles/kalyptos.css',
				'./src/styles/theme.css'
			],
			expressiveCode: {
				styleOverrides: {
					borderRadius: '0',
					borderColor: 'var(--sl-color-hairline)',
					codeFontFamily: 'var(--font-mono)',
					codeFontSize: 'var(--size-mono)',
					uiFontFamily: 'var(--font-mono)',
					frames: {
						editorTabBorderRadius: '0',
						frameBoxShadowCssValue: 'none'
					}
				}
			},
			pagination: false,
			sidebar: [
				{
					label: 'Getting started',
					items: [{ label: 'Introduction', link: '/' }, 'quickstart']
				},
				{ label: 'Endpoints', items: ['resolve', 'search'] },
				{ label: 'Reference', items: ['results', 'errors', 'data-sources'] }
			]
		}),
		mdx()
	]
});
