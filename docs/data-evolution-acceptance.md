# Data evolution -- the acceptance path

The end-to-end suite walks [data-evolution.md](data-evolution.md), stage by
stage.

A claim in that document with no step here is marketing.

A step marked `live` is one test in
[tests/e2e/test_data_evolution.py](../tests/e2e/test_data_evolution.py), with
its number in the test name.

| # | Stage | Prove | Test |
|---|---|---|---|
| 0.1 | 0 | A record with no schema or source lands in `main` | live |
| 0.2 | 0 | A nested sub-field in `_json` answers a query (`fred.nerk.frog`) | live |
| 1.1 | 1 | A pre-supplied read-only meta schema imports from dfe-schemas | live |
| 1.2 | 1 | A meta schema exports, then re-imports into a clean deployment | live, part |
| 1.3 | 1 | A field seen in `_json` promotes to a column | live |
| 2.1 | 2 | A source declares a `key=value` routing condition | live |
| 2.2 | 2 | Deploy renders the routing, the receiver reloads it in place, and records route by it | live, part |
| 2.3 | 2 | Records land in the source's OWN table, typed columns filled by dfe-loader rather than left in `_json` | live |
| 2.4 | 2 | A derived schema selects a subset, with an index type per field | live |
| 2.5 | 2 | Deploying it routes the feed to the narrower table | live |
| 2.6 | 2 | An index is added and dropped on the live table, no rebuild | live, part |
| 2.7 | 2 | A transform turns a text line in `message` into typed columns | covered by 2.8, no step of its own |
| 2.8 | 2 | The transform is swapped and the same columns still fill | live, part |
| 3.1 | 3 | A derived schema stops the loader populating `_json` and `_raw`, and the columns still EXIST on the table | live |
| 3.2 | 3 | The feed still lands with typed columns filled and `_json` empty | live |
| 3.3 | 3 | Turning population back on refills `_json` for new records -- the decision is reversible | live |

`xfail` names the open issue the step fails on. The marker is strict, so the
suite goes red the day that issue is fixed, which is how it gets removed.

`part` says the test covers less than the row claims. 2.2 asserts the DDL is
rendered and the table created. That the receiver reloads in place is the flow
suite's routing check, and that records route by it is 2.3. 2.6 applies the ALTER
directly rather than through a derived-schema edit, which is what 2.4 and 2.5
cover. 1.2 re-imports into the SAME deployment under a second path, which proves
the bundle holds what the import needs but not that it lands in a deployment
that has never seen the schema -- that needs a second stack.

2.7 has no step of its own. A source created through the API gets a transform instance with no program, because the files an operator authors into an instance are not derived, and a Compose deploy does not start the instance. 2.8 asserts the parsing on the deployment's own `filebeat` source, and [test_filebeat_pipeline.py](../tests/e2e/test_filebeat_pipeline.py) asserts it across the receiver, Kafka, transform and loader.

2.8 drives the source that has a transform instance, because Compose declares one service per transform app and binds it at deploy. So it proves a different engine on the same corpus, never a second binding. It reads that source's match rule off the API, since the source belongs to the deployment.

A derived schema stops population with `capture_json: false` and `capture_raw: false`, which the engine compiles to dfe-loader's `extracted_only`.

Three steps matter most.

**0.2 proves the pitch.** Without it we have tested insertion, not usefulness.

**1.3 is `promote`.** It answers "must I model this up front", so it is never
optional coverage.

**2.3 checks the typed columns.** A source can get a table and schema to itself and
still have every field reachable only through `_json`, which a row count alone would pass.

## The fixture

The beats MODULE level, three concurrent sources -- which proves the
receiver routing between several at once rather than one in isolation.

The three are what our pre-supplied filebeat VRL actually handles: **Cisco IOS,
Cisco Meraki and Cisco Umbrella** (`dfe-transform-vrl/pipelines/filebeat/README.md`).
The list comes from shipped code rather than being chosen.

One corpus of raw Cisco syslog feeds all three transforms -- transform-vrl,
transform-vector and transform-elastic -- and each must produce the same typed
columns.

Two constraints on that corpus:

- **Input format.** Our VRL consumes the DFE 2.1 Kafka format
  (`{message, tags, timestamp}`) with a raw syslog line in `message`.
  transform-elastic works on Elastic's ingest-pipeline format. If those differ,
  one corpus cannot feed all three unchanged.
- **Known divergence.** dfe-transform-vrl's `scripts/filebeat/extract.py` strips geoip from the
  ported VRL, so transform-elastic emits geo fields the other two never will.
  Assert around it rather than trip over it.

The VRL needs the `timezones.csv` enrichment table, and optionally
`._conf.tz_offset` and `._conf.tz_map`.

2.8 posts the Cisco IOS module alone. Widening it to all three at once turns it
into the fixture above.
