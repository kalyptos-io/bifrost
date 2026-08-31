// json pane helpers: token highlighting + the virtual window math.
// line-safe by construction: JSON.stringify escapes newlines inside string literals,
// so no token straddles a row boundary and a slice can be highlighted on its own.

// escape first, then tokenize: the entity text can never be re-matched as a json token
export function highlight(src: string): string {
	const esc = src.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
	return esc.replace(
		/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/g,
		(m, str, colon, lit) => {
			const cls = str ? (colon ? 'k' : 's') : lit ? 'l' : 'n';
			return `<span class="${cls}">${str ?? m}</span>${colon ?? ''}`;
		}
	);
}

// rows to render: what's visible plus one viewport of head and tail
export function windowRange(
	scrollTop: number,
	viewH: number,
	rowH: number,
	total: number
): { start: number; end: number } {
	if (rowH <= 0) return { start: 0, end: 0 }; // pre-layout, height not measurable yet
	const rows = Math.max(1, Math.ceil(viewH / rowH));
	const start = Math.max(0, Math.floor(scrollTop / rowH) - rows);
	return { start, end: Math.min(total, start + rows * 3) };
}
