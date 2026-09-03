---
title: Errors and limits
description: Validation errors, the batch cap, warm-up, per-item failures and the hard limits.
---

## 422 - validation

Requests are validated strictly. Unknown fields are rejected instead of ignored, so a misspelled
field name returns an error rather than falling back to a default.

```json
{ "query": "Rådhuspladsen 1", "foo": 1 }
```

```json
{
  "detail": [
    {
      "type": "extra_forbidden",
      "loc": ["body", "foo"],
      "msg": "Extra inputs are not permitted",
      "input": 1
    }
  ]
}
```

`detail` is always an array of validation entries with `type`, `loc`, `msg` and `input`. Rule
violations that span the whole body come back with `"loc": ["body"]` and a `value_error` type:

| `msg` | Cause |
| --- | --- |
| `Value error, provide query or components` | `/resolve` with neither. |
| `Value error, provide target` | `/search` in single mode without `target`. |
| `Value error, provide query` | `/search` with an empty query. |
| `Value error, item needs input or components` | A batch item with neither. |
| `Value error, lifecycle must be a non-empty list` | `"lifecycle": []`. |
| `Value error, lifecycle values must be unique` | Duplicates in `lifecycle`. |
| `Value error, unknown lifecycle values: ['nope']` | A state outside the four. |
| `Value error, unknown component keys: ['nonce']` | A `components` key outside the pinnable set. |
| `Value error, component values must be at most 128 characters` | An oversized `components` value. |
| `Value error, summed limit 6000 exceeds 5000 results` | A batch asking for too many results at once. |
| `Value error, set ['project'] per item, not top-level, for a batch query` | Batch config set at the top level. |
| `Value error, set geometry=false for a batch search` | A batch `/search` asking for geometry. |
| `Value error, set geometry=false for a batch projecting onto ['region']` | A batch `/resolve` asking for geometry on a feature projection. |

An invalid `project` or `target` fails as an enum error, and the message lists every accepted value.

The size caps are schema constraints rather than rule violations, so they come back with the field in
`loc` and a `string_too_long`, `too_long`, `greater_than_equal` or `less_than_equal` type: free text
over 512 characters, a batch over 1000 items, and `limit` outside its range (`1..20` on `/resolve`,
`1..100` on `/search`).

## 413 - body too large

A request body over 2 MB is rejected before it is parsed.

```json
{ "detail": "request body too large" }
```

## 503 - warming up

Both endpoints return `503` while the service is loading a dataset.

```json
{ "detail": "warming up" }
```

This is a transient error, usually seen right after larger updates. Retry; it clears on its own.

## 503 - overloaded

A worker sheds a request rather than queueing it once it is already at its in-flight ceiling. The
response carries `Retry-After: 1`.

```json
{ "detail": "overloaded" }
```

## 504 - request timed out

A request that has not produced a response within 30 seconds is cancelled.

```json
{ "detail": "request timed out" }
```

## Batch failures

:::caution[A batch that contains failures still returns 200]
The response is an array with one entry per request item, in request order. A failed item is an
object carrying `error` instead of `matches`.
:::

```json
[
	{ "query": "Rådhuspladsen 1, 1550 København V", "matches": [] },
	{ "query": "Nørrebrogade 1, 2200", "error": "resolution failed" }
]
```

The error string is `"resolution failed"` on `/resolve` and `"search failed"` on `/search`. There is
no per-item status code and no failure count in the envelope, so walk the array and check for the
`error` key on every entry.

Correlate by array position. `query` echoes your input, but it is `""` for an item that pinned
components instead of passing text, so position is the only reliable correlator.

An empty `matches` array is a successful response. It means the engine found nothing plausible, not
that something went wrong.

## Limits

- Body: 2 MB per request.
- Batch: 1000 items per request, on both endpoints.
- Free text: rejected over 512 characters, then truncated at 256 before parsing. Nothing in the
  response tells you about the truncation.
- `components`: only the pinnable keys, each value at most 128 characters.
- `/resolve` results: `limit`, `1..20`, default 5, and at most 5000 summed across a batch.
- `/search` results: `limit`, `1..100`, default 5, and at most 5000 summed across a batch.
- Property children: at most 1000 refs, with `relations.children.complete` reporting truncation.
- Deadline: 30 seconds per request.
- Rate limiting: none.
- Authentication: none. Front the API with your own gateway if it needs to be protected.
