<!--
  Project:   dfe-engine
  File:      docs/data-plane/hunt-match-delivery.md
  Purpose:   How a hunt or rule match is delivered, to what, and how to configure it
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# How a hunt match reaches a person

A match is a row in a ClickHouse table. When the hunt names an alert destination, the runner also sends one message for each fire that matched.

## Where a match goes

The hunt runner runs each rule of a hunt on the hunt's schedule. Every row the rule matches is written, in full, to the hunt's target table. The default is `detection` in the data database (`<data database>.detection`). A hunt or one of its rules can name another table.

Each run is capped at `hunts.max_detections_per_run` rows per rule (`DFE_ENGINE_HUNTS_MAX_DETECTIONS_PER_RUN`), and a rule hitting its cap is logged and counted in the `detections_capped` metric. The runner is described in [hunt-runner-scaling.md](hunt-runner-scaling.md) and the rules in [source-detections.md](source-detections.md).

## Who sees it

- Search the detection table in Observe, embedded in the console.
- Query it through the [query API](query-api.md).

Observe's own alerting is switched off for DFE rules, so its webhooks and alert history stay empty.

## Alert destinations

A destination is a name and an [Apprise](https://github.com/caronc/apprise) URL, which covers Slack, email, PagerDuty, Teams and about 90 other services. The URL carries its own credential, so the API masks it on every read.

Create destinations through `POST /api/v1/alerts/destinations`, then `PUT` and `DELETE` on `/api/v1/alerts/destinations/{name}`. A name starts with a letter or digit and holds letters, digits, `.`, `_` and `-`, at most 128 characters.

The engine keeps a destination in two places:

- The URL and the enabled flag, as JSON `{"url": ..., "enabled": ...}`, in the secrets backend (`settings.secrets`) at `alert-destinations/<name>`. With the `file` provider that is `<secrets.path>/alert-destinations/<name>`, with `aws` it is `<name_prefix>/alert-destinations/<name>`, and with `openbao` it is `alert-destinations/<name>` in the KV mount.
- The description and owning hunt, one YAML file per destination in `hunts.alert_destinations_dir` (`DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_DIR`). The API lists destinations from this directory, and an empty value turns the API off.

A hunt lists the destinations it wants under `alerts.destinations`, with CEL triggers over `result_count`, the rows the fire wrote:

```yaml
alerts:
  destinations:
    - slack-dfe-alerts
  triggers:
    - when: "result_count >= 10"
```

With no trigger the default is `result_count > 0`. Creating a destination with a `hunt_name` adds it to that hunt's list. Apprise URLs in `hunts.alert_channels` (`DFE_ENGINE_HUNTS_ALERT_CHANNELS`) go to every hunt.

## What the runner sends

After a fire commits its rows and its watermark, the runner checks the hunt's `alerts` block and sends one message through Apprise if a trigger holds. The message carries the hunt, the customers, the rules and `result_count`, the rows the fire wrote.

- One message per fire, however many rows it wrote. 10,000 matches in a window is one message.
- Triggers see `result_count` only. The runner does not read the matched rows back, so a trigger over a row field such as `severity` never fires.
- Destinations are read from the secrets backend on every fire, so a change through the API reaches the next fire with no restart.
- A destination that is down, or a secrets backend that cannot be read, is logged and the fire stands. The rows are already written, and re-running the window would write them twice.
- The runner reads each destination by name from the secrets backend its own `settings.secrets` names, which has to be the engine's. It needs read access to `alert-destinations/*` there and nothing of the engine's volume.

There is no cooldown yet: a hunt that matches on every fire sends a message on every fire.
