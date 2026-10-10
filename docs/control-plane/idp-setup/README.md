# Identity provider setup

One page per identity provider, written for the customer's IdP admin. The DFE operator sends the admin the page for their IdP plus two values: the DFE console host and the provider name. The admin follows the page once and sends back the client credentials, the issuer and the group identifiers.

| IdP | Page | DFE provider type | A group links on |
|---|---|---|---|
| Google Workspace | [google.md](google.md) | `google` | The Cloud Identity group id, the part after `groups/` |
| Microsoft Entra ID | [entra.md](entra.md) | `entra_id` | The group's object id (a GUID) |
| Okta | [okta.md](okta.md) | `okta` | The group name the `groups` claim carries |

Every page uses the same redirect URI, built from those two values:

```text
https://<your DFE console host>/api/v1/auth/oidc/<provider-name>/callback
```

The operator creates the provider with `POST /api/v1/auth/oidc-providers` and links each DFE group by setting its `source_id`. The provider model and the linking rules are in [rbac.md, section 4](../rbac.md#4-oidc-provider-integration).
