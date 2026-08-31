# sync

The data producer. It fetches Datafordeler / DAR fildownload and derives the tables `app` serves. It
runs locally and as an in-cluster batch job, and is never on the request path.

```sh
bifrost-sync sync        # one shot: plan -> fetch -> stage -> snapshot
bifrost-sync worker      # reconcile on an interval (the in-cluster mode)
bifrost-sync status      # the last reconcile's state, phase and error
bifrost-sync snapshot    # derive a generation only, no fetch
bifrost-sync export      # baseline corpus jsonl for train/gen
```

It needs Datafordeler OAuth credentials (`DATAFORDELER_CLIENT_ID` / `DATAFORDELER_CLIENT_SECRET`)
and a `--work-dir` for the zips and load state. Downloads resume, so a restart continues.

The producer is decoupled from deploys. `bifrost-sync` merges each entity's current state into a
persistent `datafordeler` staging schema, then derives the serving tables into a new `gen_<utc-ts>`
schema and registers it in `public.generations`. The app selects the newest generation that agrees
with its build shape and cuts over within 300 s, thus it never blocks or restarts on a reshape.
Before the first generation lands, `/resolve` gives a 503. In production the chart holds two release
units, the sync producer (`sync.enabled`) and the app consumer (`app.enabled`), and the consumer
orders on data through its serving contracts.
