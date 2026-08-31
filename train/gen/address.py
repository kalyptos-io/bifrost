"""clean address registry: dataclass + streaming jsonl reader (the input port)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Address:
    id: str
    street_name: str
    house_number: str | None
    house_letter: str | None
    floor: str | None
    door: str | None
    sub_locality: str | None
    postcode: str | None
    city: str | None

    @classmethod
    def from_json(cls, o: dict) -> Address:
        def s(v: object) -> str | None:
            return None if v is None or v == "" else str(v)

        return cls(
            id=o["id"],
            street_name=s(o.get("street_name")) or "",
            house_number=s(o.get("house_number")),
            house_letter=s(o.get("house_letter")),
            floor=s(o.get("floor")),
            door=s(o.get("door")),
            sub_locality=s(o.get("sub_locality")),
            postcode=s(o.get("postcode")),
            city=s(o.get("city")),
        )


def _in_bucket(id_: str, bucket: str | None) -> bool:
    if bucket is None:
        return True
    # blake2b not builtin hash(): hash() is salted per-process, breaking cross-run disjointness
    h = int.from_bytes(hashlib.blake2b(id_.encode(), digest_size=8).digest(), "big") % 10
    return h < 9 if bucket == "train" else h == 9


def stream(path: str | Path, bucket: str | None = None) -> Iterator[Address]:
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if not _in_bucket(o["id"], bucket):
                continue
            a = Address.from_json(o)
            # compose anchors on first present of these four (only unconditionally-rendered ones)
            if not (a.street_name or a.house_number or a.postcode or a.city):
                skipped += 1
                continue
            yield a
    if skipped:
        print(f"[!] skipped {skipped} rows with no renderable address component")
