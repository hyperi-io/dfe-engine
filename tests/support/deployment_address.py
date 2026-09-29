#  Project:      dfe-engine
#  File:         tests/support/deployment_address.py
#  Purpose:      The shape of an address one deployment chose, for tests that ship none
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An in-cluster DNS name carries the namespace one deployment was installed into.

The engine ships to deployments it has never seen, so nothing it seeds or emits may
name one. ``IN_CLUSTER_NAME`` matches ``<service>.<namespace>.svc`` with or without
the ``.cluster.local`` suffix.
"""

import re

IN_CLUSTER_NAME = re.compile(r"\.svc(\.cluster\.local)?\b")
