#  Project:      dfe-engine
#  File:         tests/e2e/filebeat_corpus.py
#  Purpose:      Read the filebeat sample corpus and wrap it for the receiver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Synthetic filebeat samples, in the shape the receiver and the pipeline expect.

The corpus is ``filebeat_cases.yaml`` beside this file: log lines written for the
repo from the documentation address ranges, each with the fields the bundled
filebeat VRL should produce. ``CORPUS`` is that file.

Two shapes have to be bridged, and doing it here rather than in the test keeps
the reason in one place:

- The samples are raw syslog and CSV LINES. The receiver routes on a field
  inside a JSON body, so a bare line carries nothing to route on.
- The bundled VRL consumes the DFE 2.1 Kafka shape, ``{message, tags,
  timestamp}``, and produces ECS.

So each line becomes ``{"message": <line>, "tags": [...], "_source": ...}``.
The discriminator is a real field the receiver's compiled rule matches, not a
test-only convention: a Source declaring ``match: {field: _source, operator:
equals, value: filebeat}`` compiles to exactly that.

``tags`` is a LIST OF STRINGS because that is what the 2.1 contract carries and
what the pipeline can read: the bundled VRL tests ``includes(array!(.tags), ...)``
for ``preserve_original_event``, and against a map it reaches
``assert!(false, "contains only works on strings and array")`` and aborts the
program for every event. Each marker is therefore a ``key:value`` string.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CORPUS = Path(__file__).with_name("filebeat_cases.yaml")

MODULES = ("cisco_umbrella", "cisco_ios", "cisco_meraki")

# The Elastic data stream each module's lines belong to.
DATASETS = {module: f"{module}.log" for module in MODULES}

# The module key a case uses in the YAML, by corpus module.
_CASE_MODULE = {"cisco_umbrella": "umbrella", "cisco_ios": "ios", "cisco_meraki": "meraki"}

# The umbrella branch needs no timezone table, so it is the subset that runs
# without the enrichment tables mounted.
NO_ENRICHMENT_MODULE = "cisco_umbrella"


@dataclass(frozen=True, slots=True)
class Sample:
    """One raw log line, and where it came from."""

    module: str
    name: str
    line: str
    index: int

    @property
    def marker(self) -> str:
        """A value unique to this sample, for finding it again downstream."""
        return f"{self.module}/{self.name}#{self.index}"


def available(path: Path | None = None) -> bool:
    """Whether the corpus file is present."""
    return (path or CORPUS).is_file()


def _cases(path: Path | None) -> list[dict]:
    loaded = yaml.safe_load((path or CORPUS).read_text(encoding="utf-8"))
    return loaded or []


def samples(
    path: Path | None = None, *, modules: tuple[str, ...] = MODULES, limit: int = 0
) -> list[Sample]:
    """Every raw line in the corpus, grouped by module in ``modules`` order.

    ``limit`` caps the count PER MODULE rather than overall, so a reduced run
    still exercises every branch instead of stopping inside the first one.
    """
    cases = _cases(path)
    out: list[Sample] = []
    for module in modules:
        key = _CASE_MODULE[module]
        count = 0
        for case in cases:
            if case["module"] != key:
                continue
            if limit and count >= limit:
                break
            out.append(
                Sample(module=module, name=f"{case['name']}.log", line=case["line"], index=0)
            )
            count += 1
    return out


def goldens(path: Path | None = None, *, modules: tuple[str, ...] = MODULES) -> dict[str, dict]:
    """The expected output of each case, keyed as ``<sample name>-expected.json``."""
    wanted = {_CASE_MODULE[m] for m in modules}
    return {
        f"{case['name']}.log-expected.json": {"expected": [case["expect"]]}
        for case in _cases(path)
        if case["module"] in wanted
    }


def wrap(sample: Sample, source: str = "filebeat", run: str = "") -> dict:
    """One sample as the JSON body the receiver routes and the VRL consumes.

    ``run`` tags every event of one run so a shared cluster's existing rows are
    not mistaken for this run's output.
    """
    tags: list[str] = [f"corpus_module:{sample.module}", f"corpus_marker:{sample.marker}"]
    if run:
        tags.append(f"e2e_run:{run}")
    return {"message": sample.line, "tags": tags, "_source": source}


def wrap_all(items: list[Sample], source: str = "filebeat", run: str = "") -> list[dict]:
    """Every sample wrapped, in corpus order."""
    return [wrap(s, source=source, run=run) for s in items]
