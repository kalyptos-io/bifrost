"""raw csv -> canonical staging rows. streams a zip's csv, applies the currency predicate, and
shapes a row to a spec (keep-list projection + ascii rename + Kind conversion, wkt -> geojson).

pure and per-file: sniffer state is resolved once per file (headers are stable within a file, but
vary across data-model versions), so reduce hands each file a fresh SniffState.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime

from .registers import Column, Currency, EntitySpec, Kind

# dar wkt road-geometry fields exceed python's 128k csv field cap
csv.field_size_limit(10**9)

# a register point is wkt: "POINT(722345.67 6179535.68)" in epsg:25832 (easting northing)
_POINT = re.compile(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.IGNORECASE)


def clean(v: object) -> str | None:
    return (str(v) if v is not None else "").strip() or None


def to_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def to_utc_iso(v: object) -> str | None:
    """iso timestamp (any offset, trailing Z, or naive) -> one canonical utc iso; unparseable ->
    None. the single canonical form the fold's max() and the snapshot point-in-time text joins
    both compare, so lexical order matches chronological order across offsets/precision."""
    s = clean(v)
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # naive -> assume utc
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# csv streaming


def _prepend(first: str, rest: Iterator[str]) -> Iterator[str]:
    yield first
    yield from rest


def zip_rows(zip_path: str) -> Iterator[dict]:
    """stream the single csv member of a datafordeler entity zip as dict rows."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise SystemExit(f"[!] no csv member in {zip_path}")
        with z.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            header = text.readline()
            if not header:
                return
            # sniff , vs ; (csv is undocumented; agency uses ;, danish locale)
            delim = ";" if header.count(";") > header.count(",") else ","
            yield from csv.DictReader(_prepend(header, text), delimiter=delim)


# delta-fold headers (raw, never staged on the main tables): the fold reads these to select the
# registration-current / virkning-latest version. lifecycle itself is classified later in snapshot
# sql from the staged status/aktualitet columns - extract classifies nothing.


def fold_headers(spec: EntitySpec) -> tuple[str, ...]:
    """raw headers the delta fold requires; a delta missing them aborts rather than fail-open.
    registreringTil discriminates registration-superseded corrections; virkningTil is the dagi
    lifecycle window (missing -> every closed area fails open to current), required wherever staged.
    virkningFra/registreringFra merely order versions, read opportunistically."""
    if spec.currency is Currency.AKTUALITET:
        return ("aktualitet",)
    if any(c.name == "virkningtil" for c in spec.columns):
        return ("registreringTil", "virkningTil")
    return ("registreringTil",)


def pk_value(row: dict, spec: EntitySpec) -> str | None:
    """the row's identity under the spec's source pk, cleaned to match the shaped pk column."""
    return clean(row.get(spec.pk))


# wkt -> geojson (stdlib; geo libs never reach the distroless app). handles the bounded set we emit:
# (MULTI)POINT/LINESTRING/POLYGON, optional Z/M (extra ordinates dropped). paren nesting maps 1:1 to
# geojson nesting; the leaf is a comma-separated run of "x y [z]" points.
_WKT_HEAD = re.compile(r"^\s*([A-Za-z]+)\s*(?:ZM?|M)?\s*", re.IGNORECASE)
_GEOJSON_TYPE = {
    "POINT": "Point",
    "LINESTRING": "LineString",
    "POLYGON": "Polygon",
    "MULTIPOINT": "MultiPoint",
    "MULTILINESTRING": "MultiLineString",
    "MULTIPOLYGON": "MultiPolygon",
}


def _split_top(s: str) -> list[str]:
    # split on commas at paren depth 0
    out, depth, start = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(s[start:i])
            start = i + 1
    out.append(s[start:])
    return out


def _parse_group(s: str) -> list:
    s = s.strip()
    if not (s.startswith("(") and s.endswith(")")):
        raise ValueError("unbalanced wkt group")
    inner = s[1:-1].strip()
    if inner.startswith("("):  # nested groups (rings / parts)
        return [_parse_group(g) for g in _split_top(inner)]
    return [[float(t) for t in p.split()[:2]] for p in inner.split(",")]  # leaf points, x y only


def _wkt_to_geojson(wkt: str | None) -> dict | None:
    if not wkt:
        return None
    m = _WKT_HEAD.match(wkt)
    if not m:
        return None
    token = m.group(1).upper()
    # greedy capture glues a flag-less z/m to the type (polygonz); strip the dim flag on a miss
    gtype = _GEOJSON_TYPE.get(token) or _GEOJSON_TYPE.get(token.rstrip("ZM"))
    body = wkt[m.end() :].strip()
    if gtype is None or not body.startswith("("):
        return None  # EMPTY, or a type we don't emit
    try:
        coords: object = _parse_group(body)
    except (ValueError, IndexError):
        return None
    if gtype == "Point":  # "(x y)" parses as [[x, y]]; geojson Point wants [x, y]
        coords = coords[0]
    return {"type": gtype, "coordinates": coords}


def _wkt_xy(wkt: str | None) -> tuple[float, float] | None:
    m = _POINT.match(wkt.strip()) if wkt else None
    return (float(m.group(1)), float(m.group(2))) if m else None


def _point_text(wkt: str | None) -> str | None:
    # register point text is already "easting northing"; keep the coord text verbatim (no reformat)
    m = _POINT.match(wkt.strip()) if wkt else None
    return f"{m.group(1)} {m.group(2)}" if m else None


def _geojson_text(wkt: str | None) -> str | None:
    geo = _wkt_to_geojson(wkt)
    return json.dumps(geo, ensure_ascii=False, separators=(",", ":")) if geo else None


# shaping


class SniffState:
    """resolves each value-sniffed column to a concrete header, once per file. resolution is lazy:
    a sniffer inspects values, so it waits for a row that actually carries the geometry/point."""

    def __init__(self, spec: EntitySpec):
        self._sniffers = {i: c.src for i, c in enumerate(spec.columns) if callable(c.src)}
        self._resolved: dict[int, str] = {}

    def resolve(self, index: int, row: dict) -> str | None:
        cached = self._resolved.get(index)
        if cached is not None:
            return cached
        col = self._sniffers[index](row)  # type: ignore[operator]
        if col is not None:
            self._resolved[index] = col
        return col


def _raw(row: dict, col: Column, sniffed: SniffState, index: int) -> object:
    src = col.src
    if callable(src):
        resolved = sniffed.resolve(index, row)
        return row.get(resolved) if resolved else None
    if isinstance(src, tuple):  # dialect variants: first present header wins
        return next((row[v] for v in src if v in row), None)
    return row.get(src)


def _emit(out: dict, col: Column, raw: object) -> None:
    kind = col.kind
    if kind is Kind.POINT_XY:
        xy = _wkt_xy(raw)  # type: ignore[arg-type]
        kx, ky = col.name  # a 2-tuple for POINT_XY
        out[kx], out[ky] = (xy[0], xy[1]) if xy else (None, None)
        return
    if kind is Kind.TEXT:
        value: object = clean(raw)
    elif kind is Kind.DOUBLE:
        value = to_float(raw)
    elif kind is Kind.GEOJSON:
        value = _geojson_text(raw)  # type: ignore[arg-type]
    elif kind is Kind.POINT_TEXT:
        value = _point_text(raw)  # type: ignore[arg-type]
    elif kind is Kind.TIMESTAMP:
        value = to_utc_iso(raw)
    else:  # pragma: no cover - exhaustive over Kind
        raise ValueError(kind)
    out[col.name] = value


def shape_row(row: dict, spec: EntitySpec, sniffed: SniffState) -> dict:
    """project a raw csv row onto the spec's keep-list, rename to canonical ascii, convert by Kind.

    every canonical column is present (missing/unparseable -> None) so the staging shape is stable.
    """
    out: dict = {}
    for i, col in enumerate(spec.columns):
        _emit(out, col, _raw(row, col, sniffed, i))
    return out
