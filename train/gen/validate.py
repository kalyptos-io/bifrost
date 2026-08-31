"""validate generated corpus structure, coverage, normalization, and split isolation."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

from bifrost.arms.normalize import normalize

from .mutate import _TIER, _name

_FIELDS = {
    "street_name": "street",
    "house_number": "house_number",
    "house_letter": "house_letter",
    "floor": "floor",
    "door": "door",
    "sub_locality": "sub_locality",
    "postcode": "postcode",
    "city": "city",
}
_STRUCTURAL = {
    "field_drop",
    "field_skip",
    "prefix_cut",
    "junk_only",
    "invalid_postcode",
    "invalid_house_number",
}
_NO_TARGET = {"junk_only", "invalid_postcode", "invalid_house_number"}
_CONFIGURED = {_name(family) for families in _TIER.values() for family, _ in families} | _STRUCTURAL
_REQUIRED = frozenset(
    {"raw", "normalized", "target", "spans", "tier", "intensity", "noise_level", "mutations"}
)


def _read(path: str | Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as file:
        for line in file:
            if line.strip():
                yield json.loads(line)


def _span_labels(record: dict, prefix: str, errors: list[str]) -> set[str]:
    labels = set()
    end = 0
    for span in record["spans"]:
        start, stop = span["start"], span["end"]
        if not 0 <= start < stop <= len(record["normalized"]) or start < end:
            errors.append(f"{prefix}: invalid span bounds")
            break
        labels.add(span["label"])
        end = stop
    return labels


def _validate_target(record: dict, labels: set[str], prefix: str, errors: list[str]) -> str | None:
    target = record["target"]
    if target is None:
        if not set(record["mutations"]) & _NO_TARGET:
            errors.append(f"{prefix}: no-target record lacks a no-target mutation")
        return None
    target_id = target.get("id")
    if not target_id:
        errors.append(f"{prefix}: target id is empty")
    for field, label in _FIELDS.items():
        if bool(target.get(field)) != (label in labels):
            errors.append(f"{prefix}: target field {field} differs from spans")
            break
    return target_id


def validate_records(
    records: Iterable[dict], require_coverage: bool = True
) -> tuple[dict, list[str]]:
    errors: list[str] = []
    mutations = Counter()
    tiers = Counter()
    target_ids = set()
    no_target = 0
    record_count = 0
    for index, record in enumerate(records):
        record_count += 1
        prefix = f"record {index}"
        if missing := _REQUIRED - record.keys():
            errors.append(f"{prefix}: missing {sorted(missing)}")
            continue
        if normalize(record["raw"]) != record["normalized"]:
            errors.append(f"{prefix}: normalized surface differs from serving normalization")
        names = record["mutations"]
        if record["noise_level"] != len(names):
            errors.append(f"{prefix}: noise level differs from mutation count")
        if len(names) != len(set(names)):
            errors.append(f"{prefix}: duplicate mutation names")
        mutations.update(names)
        tiers[record["tier"]] += 1
        target_id = _validate_target(record, _span_labels(record, prefix, errors), prefix, errors)
        if record["target"] is None:
            no_target += 1
        elif target_id:
            target_ids.add(target_id)
    if require_coverage:
        if set(tiers) != {1, 2, 3}:
            errors.append(f"missing tiers: {sorted({1, 2, 3} - set(tiers))}")
        if missing := _CONFIGURED - mutations.keys():
            errors.append(f"unreached mutations: {sorted(missing)}")
        if not no_target:
            errors.append("no no-target records")
    return {
        "records": record_count,
        "targets": record_count - no_target,
        "no_target": no_target,
        "tiers": dict(sorted(tiers.items())),
        "mutations": dict(sorted(mutations.items())),
        "target_ids": target_ids,
    }, errors


def validate_files(train_path: str, held_path: str) -> tuple[dict, list[str]]:
    train, train_errors = validate_records(_read(train_path))
    held, held_errors = validate_records(_read(held_path))
    overlap = train["target_ids"] & held["target_ids"]
    errors = [
        *(f"train: {error}" for error in train_errors),
        *(f"held: {error}" for error in held_errors),
    ]
    if overlap:
        errors.append(f"train/held target overlap: {len(overlap)}")
    for summary in (train, held):
        summary["unique_target_ids"] = len(summary.pop("target_ids"))
    return {"train": train, "held": held}, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="validate generated train and held-out corpora")
    parser.add_argument("--train", required=True)
    parser.add_argument("--held", required=True)
    args = parser.parse_args()
    summary, errors = validate_files(args.train, args.held)
    print(json.dumps(summary, indent=2))
    if errors:
        for error in errors:
            print(f"[-] {error}")
        raise SystemExit(1)
    print("[+] generated corpora are valid and disjoint")


if __name__ == "__main__":
    main()
