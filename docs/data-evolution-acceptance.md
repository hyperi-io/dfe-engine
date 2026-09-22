# Data evolution -- the acceptance path

The end-to-end suite walks [data-evolution.md](data-evolution.md), stage by
stage.

A claim in that document with no step here is marketing.

## The steps

A step marked `live` is one test in
[tests/e2e/test_data_evolution.py](../tests/e2e/test_data_evolution.py), named
for its number.

| # | Stage | Prove | Test |
|---|---|---|---|
| 0.1 | 0 | A record with no schema or source lands in `main` | live |
| 0.2 | 0 | A nested sub-field in `_json` answers a query (`fred.nerk.frog`) | live |
| 1.1 | 1 | A pre-supplied read-only meta schema imports from dfe-schemas | live |
| 1.2 | 1 | A meta schema exports, then re-imports into a clean deployment | none |
| 1.3 | 1 | A field seen in `_json` promotes to a real column | live, xfail dfe-engine#459 |
| 2.1 | 2 | A source declares a `key=value` routing condition | live |
| 2.2 | 2 | Deploy renders the routing, the receiver reloads it in place, and records route by it | live, part |
| 2.3 | 2 | Records land in the source's OWN table, typed columns filled by dfe-loader rather than left in `_json` | live |
| 2.4 | 2 | A derived schema selects a subset, with an index type per field | none |
| 2.5 | 2 | Deploying it routes the feed to the narrower table | none |
| 2.6 | 2 | An index is added and dropped on the live table, no rebuild | live, part |
| 2.7 | 2 | A transform turns a text line in `message` into typed columns | live, xfail dfe-loader#184 |
| 2.8 | 2 | The transform is swapped and the same columns still fill | none |
| 3.1 | 3 | A derived schema stops the loader populating `_json` and `_raw`, and the columns still EXIST on the table | none |
| 3.2 | 3 | The feed still lands with typed columns filled and `_json` empty | none |
| 3.3 | 3 | Turning population back on refills `_json` for new records -- the decision is reversible | none |

## What the marks mean

`xfail` names the open issue the step fails on. The marker is strict, so the
suite goes red the day that issue is fixed, which is how it gets removed.

`part` says the test covers less than the row claims. 2.2 asserts the DDL is
rendered and the table created; that the receiver reloads in place is the flow
suite's routing check, and that records route by it is 2.3. 2.6 applies the ALTER
directly rather than through a derived-schema edit, because the CRUD that would
emit it is 2.4 and 2.5.

`none` is a step with no test, not a step that passes. 1.2, 2.4, 2.5 and 2.8 are
unwritten. The three stage 3 steps cannot pass at all: a derived schema has no
way to say "stop populating `_json`", so the engine never compiles it.

## Three carry the weight

**0.2 proves the pitch.** Without it we have tested insertion, not usefulness.

**1.3 is `promote`.** It answers "must I model this up front", so it is never
optional coverage.

**2.3 is the sharp one.** A source can get its own table and schema and still
have every field reachable only through `_json`. Count rows and it passes. Read
the typed columns and it does not.

## The fixture

The beats MODULE level, three concurrent sources -- which also proves the
receiver routing between several at once rather than one in isolation.

The three are what our pre-supplied filebeat VRL actually handles: **Cisco IOS,
Cisco Meraki and Cisco Umbrella** (`dfe-transform-vrl/pipelines/filebeat/README.md`).
The list comes from shipped code rather than being chosen.

One corpus of raw Cisco syslog feeds all three transforms -- transform-vrl,
transform-vector and transform-elastic -- and each must produce the same typed
columns.

Two things to settle before building:

- **Input shape.** Our VRL consumes the DFE 2.1 Kafka shape
  (`{message, tags, timestamp}`) with a raw syslog line in `message`.
  transform-elastic works on Elastic's ingest-pipeline shape. If those differ,
  one corpus cannot feed all three unchanged.
- **Known divergence.** `scripts/filebeat/extract.py` strips geoip from the
  ported VRL, so transform-elastic emits geo fields the other two never will.
  Assert around it rather than trip over it.

The VRL also needs the `timezones.csv` enrichment table, and optionally
`._conf.tz_offset` and `._conf.tz_map`.

That is the target. 2.7 posts the Cisco IOS module alone today, through whichever
transform app `DFE_E2E_TRANSFORM` names; widening it to all three at once is what
turns it into the fixture above.
