#  Project:      dfe-engine
#  File:         tests/e2e/filebeat_corpus.py
#  Purpose:      Read the filebeat sample corpus and wrap it for the receiver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real filebeat samples, in the shape the receiver and the pipeline expect.

The corpus is dfe-transform-vrl's, pinned to elastic/integrations @ c7bc530 -
the same vintage the bundled filebeat VRL was generated against, so its golden
outputs are the reference for what the transform should produce.

Two shapes have to be bridged, and doing it here rather than in the test keeps
the reason in one place:

- The samples are raw syslog and CSV LINES. The receiver routes on a field
  inside a JSON body, so a bare line carries nothing to route on.
- The bundled VRL consumes the DFE 2.1 Kafka shape, ``{message, tags,
  timestamp}``, and produces ECS.

So each line becomes ``{"message": <line>, "tags": [], "_source": ...,
"_e2e": {...}}``. The discriminator is a real field the receiver's compiled
rule matches, not a test-only convention: a Source declaring ``match: {field:
_source, operator: equals, value: filebeat}`` compiles to exactly that.

The run marker sits at the ROOT, not in ``tags``: every module branch of the
bundled pipeline assigns ``.tags`` outright (filebeat.vrl:130, 2027, 3021), so
a marker inside it is gone by the time the row lands and nothing downstream can
find this run's events.

Read straight out of the archive, never unpacked to the working tree - it
carries Elastic-licensed data whose terms travel with it.
"""

from __future__ import annotations

import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CORPUS = Path(
    "/projects/dfe-transform-vrl/tests/fixtures/filebeat/filebeat-testdata.tar.gz"
)

# The corpus lives in the transform repo, so a checkout anywhere else needs to
# say where.
CORPUS = Path(os.environ.get("DFE_FILEBEAT_CORPUS") or DEFAULT_CORPUS)

MODULES = ("cisco_umbrella", "cisco_ios", "cisco_meraki")

# Root field carrying the replay's own metadata through the transform.
MARKER_FIELD = "_e2e"

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
    """Whether the corpus archive is present."""
    return (path or CORPUS).is_file()


def samples(
    path: Path | None = None, *, modules: tuple[str, ...] = MODULES, limit: int = 0
) -> list[Sample]:
    """Every raw line in the corpus, oldest module first.

    ``limit`` caps the count PER MODULE rather than overall, so a reduced run
    still exercises every branch instead of stopping inside the first one.
    """
    archive = path or CORPUS
    out: list[Sample] = []
    per_module: dict[str, int] = dict.fromkeys(modules, 0)
    with tarfile.open(archive, "r:gz") as tar:
        for member in sorted(tar.getmembers(), key=lambda m: m.name):
            module = _module_of(member.name, modules)
            if module is None or not member.name.endswith(".log"):
                continue
            if limit and per_module[module] >= limit:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            body = handle.read().decode("utf-8", errors="replace")
            for i, line in enumerate(body.splitlines()):
                if not line.strip():
                    continue
                if limit and per_module[module] >= limit:
                    break
                out.append(
                    Sample(
                        module=module,
                        name=Path(member.name).name,
                        line=line,
                        index=i,
                    )
                )
                per_module[module] += 1
    return out


def goldens(path: Path | None = None, *, modules: tuple[str, ...] = MODULES) -> dict[str, dict]:
    """The upstream expected outputs, keyed by their sample file's name."""
    archive = path or CORPUS
    out: dict[str, dict] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith(".log-expected.json"):
                continue
            if _module_of(member.name, modules) is None:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            try:
                out[Path(member.name).name] = json.loads(handle.read().decode("utf-8"))
            except json.JSONDecodeError:
                continue
    return out


def _module_of(name: str, modules: tuple[str, ...]) -> str | None:
    for module in modules:
        if name.startswith(f"{module}/"):
            return module
    return None


def wrap(sample: Sample, source: str = "filebeat", run: str = "") -> dict:
    """One sample as the JSON body the receiver routes and the VRL consumes.

    ``run`` marks every event of one run so a shared cluster's existing rows are
    not mistaken for this run's output. ``tags`` is an empty list because that is
    the shape the pipeline expects to find and replace.
    """
    marker: dict[str, str] = {"module": sample.module, "marker": sample.marker}
    if run:
        marker["run"] = run
    return {
        "message": sample.line,
        "tags": [],
        "_source": source,
        MARKER_FIELD: marker,
    }


def wrap_all(items: list[Sample], source: str = "filebeat", run: str = "") -> list[dict]:
    """Every sample wrapped, in corpus order."""
    return [wrap(s, source=source, run=run) for s in items]
