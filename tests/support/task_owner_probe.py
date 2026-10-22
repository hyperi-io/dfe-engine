#  Project:      dfe-engine
#  File:         tests/support/task_owner_probe.py
#  Purpose:      An engine replica that owns one endless task until it is killed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Own one endless task in the document store, heartbeating it, until killed.

``tests/integration/test_document_tasks.py`` runs this as a child and SIGKILLs it, so
the task's owner dies the way a crashed replica does: nothing records how the task
ended. The store comes from ``DFE_TEST_TASK_STORE_URI`` and ``DFE_TEST_TASK_STORE_DATABASE``
rather than argv, so the URI's password never shows in a process listing. The name keeps
it out of normal collection.
"""

import asyncio
import os

from dfe_engine.api.document_tasks import DocumentTaskManager
from dfe_engine.store.documents import DocuStore

OWNER = "replica-killed"
KIND = "test:owner-killed"


async def _forever(*, task) -> None:
    await asyncio.Event().wait()


async def main() -> None:
    store = DocuStore(
        os.environ["DFE_TEST_TASK_STORE_URI"], os.environ["DFE_TEST_TASK_STORE_DATABASE"]
    )
    manager = DocumentTaskManager(store, owner=OWNER)
    manager.submit(KIND, _forever)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
