"""interactive playground. two modes, picked with --mode (default: resolve):
  resolve  - type a noisy address, watch the engine's ranked matches live (POST /resolve)
  segment  - type an address, see it coloured by the segmenter's labels live
run from train/: uv run python -m model.playground [--mode resolve|segment]"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from bifrost.arms.normalize import normalize
from bifrost.arms.segmenter import LABELS, load, segment
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

_ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "data" / "artifacts"
URL = os.environ.get("BIFROST_URL", "http://localhost:8000").rstrip("/") + "/resolve"

# explicit truecolor (okabe-ito, colorblind-safe) so hues don't collide via terminal theme
STYLE: dict[str, str] = {
    "street": "bold #009e73",
    "house_number": "bold #f0e442",
    "house_letter": "bold #56b4e9",
    "floor": "bold #e69f00",
    "door": "bold #0072b2",
    "sub_locality": "bold #999999",
    "postcode": "bold #d55e00",
    "city": "bold #cc79a7",
    "junk": "dim strike",
}

TAG: dict[str, str] = {
    "street": "st",
    "house_number": "num",
    "house_letter": "ltr",
    "floor": "flr",
    "door": "dr",
    "sub_locality": "sub",
    "postcode": "post",
    "city": "city",
    "junk": "junk",
}

# confidence -> okabe-ito hue (A good, B caution, C weak)
CONF = {"A": "bold #009e73", "B": "bold #e69f00", "C": "bold #d55e00"}

CANNED = [
    "strandersgade 2100",
    "strandersgade koebenha",
    "hovedstien",
    "hovedstien 2",
    "soeborg hovedgade 177c 3 th 2860 soeborg !!!!!!",
    "soeborg hovedgade 177c 3 th 2860 soeborg ;oalkmwdlk;xmjklajknwdb",
]


# ---- segment mode ----


def legend() -> Text:
    t = Text()
    for label in LABELS:
        t.append(f" {label} ", STYLE[label])
    return t


def apply_spans(raw: str, spans: list[tuple[str, int, int]]) -> Text:
    t = Text(raw)
    for label, s, e in spans:
        t.stylize(STYLE[label], s, e)
    return t


def compact(raw: str, spans: list[tuple[str, int, int]]) -> Text:
    # [tag]spantext per labeled span; .plain is the copyable string
    t = Text()
    for label, s, e in spans:
        t.append(f"[{TAG[label]}]", STYLE[label])
        t.append(raw[s:e])
    return t


class SegmentPlayground(App):
    TITLE = "segmenter playground"
    CSS = """
    #legend { padding: 1 2; }
    #out { padding: 0 2; min-height: 3; }
    #compact { padding: 1 2; border-top: dashed grey; }
    #canned { padding: 1 2; border-top: dashed grey; }
    Input { margin: 1 2; }
    """

    def compose(self) -> ComposeResult:
        yield Static(legend(), id="legend")
        yield Static(id="out")
        yield Static(id="compact")
        yield Static(id="canned")
        yield Input(placeholder="type an address…")

    def on_mount(self) -> None:
        load(_ARTIFACT_DIR)  # front-load the onnx session so the first keystroke isn't slow
        t = Text()
        for raw in CANNED:
            norm = normalize(raw)
            spans = segment(norm)
            t.append(raw + "\n", "dim")  # source, before normalize
            t.append(apply_spans(norm, spans))
            t.append("\n")
            t.append(compact(norm, spans))
            t.append("\n\n")
        self.query_one("#canned", Static).update(t)
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        norm = normalize(event.value)  # color what the model sees, not the raw input
        spans = segment(norm)
        self.query_one("#out", Static).update(apply_spans(norm, spans))
        self.query_one("#compact", Static).update(compact(norm, spans))


# ---- resolve mode ----


def _conf(m: dict) -> str:
    return m["meta"].get("confidence") or "A"  # wire omits confidence on a clean A


def render(data: dict) -> Text:
    matches = data.get("matches", [])
    if not matches:
        return Text("no match", style="dim")
    top = matches[0]
    t = Text(top["result"] + "\n", style="bold")
    t.append(_conf(top), CONF.get(_conf(top), "dim"))
    t.append(f"  ·  {top['meta']['score']:.2f}\n", style="dim")
    for m in matches[1:4]:
        t.append(f"\n{m['result']}  [{_conf(m)}]", style="dim")
    return t


def _geom_type(data: dict) -> str | None:
    matches = data.get("matches", [])
    geom = matches[0].get("geometry") if matches else None
    return geom["geojson"]["type"] if geom else None


def status(autocomplete: bool, ms: float | None = None, geom: str | None = None) -> Text:
    t = Text()
    t.append("autocomplete ", style="dim")
    t.append("ON" if autocomplete else "OFF", CONF["A"] if autocomplete else CONF["C"])
    t.append("  ·  tab toggles", style="dim")
    if ms is not None:
        t.append(f"   {ms:.1f} ms", style="bold")
    if geom:
        t.append(f"  ·  {geom}", style="dim")
    return t


def _post(query: str, autocomplete: bool) -> tuple[dict, float]:
    body = json.dumps({"query": query, "target": "address" if autocomplete else "auto"}).encode()
    req = urllib.request.Request(URL, body, {"content-type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r), (time.perf_counter() - t0) * 1000


class ResolvePlayground(App):
    TITLE = "bifrost resolve playground"
    BINDINGS = [Binding("tab", "toggle_autocomplete", "autocomplete", priority=True)]
    CSS = """
    #status { dock: top; padding: 1 2; }
    #result { width: 100%; height: 1fr; content-align: center middle; padding: 1 2; }
    Input { dock: bottom; margin: 1 2; }
    """

    autocomplete = True

    def compose(self) -> ComposeResult:
        yield Static(status(self.autocomplete), id="status")
        yield Static(Text("type an address…", style="dim"), id="result")
        yield Input(placeholder="type a noisy address…")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_toggle_autocomplete(self) -> None:
        self.autocomplete = not self.autocomplete
        self.query_one("#status", Static).update(status(self.autocomplete))
        if q := self.query_one(Input).value.strip():
            self._resolve(q)

    def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.strip()
        if not q:
            self.query_one("#result", Static).update(Text("type an address…", style="dim"))
            self.query_one("#status", Static).update(status(self.autocomplete))
            return
        self._resolve(q)  # no debounce; exclusive worker drops the stale in-flight request

    @work(exclusive=True)
    async def _resolve(self, query: str) -> None:
        try:
            data, ms = await asyncio.to_thread(_post, query, self.autocomplete)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self.query_one("#result", Static).update(
                Text("engine unreachable - is the stack up?", style="bold #d55e00")
            )
            return
        self.query_one("#result", Static).update(render(data))
        self.query_one("#status", Static).update(status(self.autocomplete, ms, _geom_type(data)))


def main() -> None:
    p = argparse.ArgumentParser(description="bifrost playground (resolve | segment)")
    p.add_argument("--mode", choices=["resolve", "segment"], default="resolve")
    app = ResolvePlayground() if p.parse_args().mode == "resolve" else SegmentPlayground()
    app.run()


if __name__ == "__main__":
    main()
