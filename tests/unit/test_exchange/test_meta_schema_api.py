#  Project:      dfe-engine
#  File:         tests/unit/test_exchange/test_meta_schema_api.py
#  Purpose:      Acceptance step 1.2 over HTTP -- export a meta schema, re-import it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The meta-schema export and import routes, deployment to deployment.

This is acceptance step 1.2 in ``docs/data-evolution-acceptance.md``: a meta
schema exports, then re-imports into a clean deployment.
"""

from __future__ import annotations

from tests.unit.test_exchange.conftest import CORE_SCHEMA, CUSTOM_SCHEMA

EXPORT_URL = "/api/v1/schemas/definitions/meta/cisco_ios/export"
CORE_EXPORT_URL = "/api/v1/schemas/definitions/meta/core_tpl/export"


def _alpha(deployment):
    return deployment(
        "alpha", schemas={"meta/cisco_ios": CUSTOM_SCHEMA, "meta/core_tpl": CORE_SCHEMA}
    )


class TestExportRoute:
    def test_a_user_authored_schema_exports_in_full(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            exported = client.get(EXPORT_URL, headers=headers)

        assert exported.status_code == 200
        document = exported.json()
        assert document["kind"] == "meta_schema"
        assert document["resource_type"] == "custom"
        assert document["current"] == "1.1.0"
        assert sorted(document["versions"]) == ["1.0.0", "1.1.0"]
        assert "reference" not in document

    def test_a_core_schema_exports_as_a_reference_with_a_pin(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            exported = client.get(CORE_EXPORT_URL, headers=headers)

        assert exported.status_code == 200
        document = exported.json()
        assert document["resource_type"] == "core"
        assert document["reference"] == {"path": "meta/core_tpl", "version": "2.0.0"}
        assert "versions" not in document
        assert "observed_at" not in exported.text

    def test_a_schema_that_is_not_there_is_a_404(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            exported = client.get("/api/v1/schemas/definitions/meta/absent/export", headers=headers)

        assert exported.status_code == 404


class TestImportRoute:
    def test_export_then_import_into_a_clean_deployment(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            document = client.get(EXPORT_URL, headers=headers).json()

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/schemas/import", json=document, headers=headers)
            assert imported.status_code == 201, imported.text
            round_tripped = client.get(EXPORT_URL, headers=headers).json()

        assert imported.json() == {
            "path": "meta/cisco_ios",
            "resource_type": "custom",
            "action": "created",
            "version": "1.1.0",
        }
        assert round_tripped == document

    def test_a_core_document_resolves_without_writing(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            document = client.get(CORE_EXPORT_URL, headers=headers).json()

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            before = {
                entry["name"]
                for entry in client.get("/api/v1/schemas", headers=headers).json()["items"]
            }
            imported = client.post("/api/v1/schemas/import", json=document, headers=headers)
            after = {
                entry["name"]
                for entry in client.get("/api/v1/schemas", headers=headers).json()["items"]
            }

        assert imported.status_code == 201
        assert imported.json()["action"] == "resolved"
        assert after == before

    def test_a_core_document_the_deployment_lacks_is_a_422(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            document = client.get(CORE_EXPORT_URL, headers=headers).json()

        client, headers = deployment("beta", schemas={})
        with client:
            imported = client.post("/api/v1/schemas/import", json=document, headers=headers)

        assert imported.status_code == 422
        assert imported.json()["code"] == "unresolved_reference"

    def test_a_document_that_embeds_a_core_schema_is_refused_at_the_body(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            document = client.get(EXPORT_URL, headers=headers).json()
            document["resource_type"] = "core"
            document["reference"] = {"path": "meta/cisco_ios", "version": "1.1.0"}
            refused = client.post("/api/v1/schemas/import", json=document, headers=headers)

        assert refused.status_code == 422
        assert "forks" in refused.text

    def test_importing_twice_is_a_409(self, deployment):
        client, headers = _alpha(deployment)
        with client:
            document = client.get(EXPORT_URL, headers=headers).json()

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            first = client.post("/api/v1/schemas/import", json=document, headers=headers)
            again = client.post("/api/v1/schemas/import", json=document, headers=headers)

        assert first.status_code == 201
        assert again.status_code == 409
        assert again.json()["code"] == "conflict"
