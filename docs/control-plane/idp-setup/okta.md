# Okta sign-in for DFE

DFE asks for two things from your Okta org: your users sign in to DFE with their Okta account, and DFE reads which Okta groups each user is in, so your groups decide their DFE roles. You set this up once as an app integration plus a groups claim. Nothing is needed per user.

Console paths checked 2026-10-10 against Okta's documentation.

The DFE operator who sent you this page gives you the redirect URI. It has this shape, and you paste it exactly as given:

```text
https://<your DFE console host>/api/v1/auth/oidc/<provider-name>/callback
```

You need an Okta administrator who can create app integrations and edit authorization servers.

## How DFE reads groups

Okta puts no groups in its tokens until you add a `groups` claim. Once you do, the ID token carries the names of the user's groups that match your filter, and DFE reads them at sign-in. DFE does not call the Okta API for this. DFE reads groups only from the ID token, never from the userinfo endpoint, so the claim must always be in the ID token.

Put the claim on Okta's default custom authorization server, named `default`, with the issuer `https://<your Okta domain>/oauth2/default`. The org authorization server (issuer `https://<your Okta domain>`, no `/oauth2/...`) sends no groups claim in the ID token, so DFE sees no groups through it.

A production Okta org has custom authorization servers only with API Access Management. Integrator Free Plan orgs have it. Without it, DFE's only other route is reading groups from the Okta API with an API token (see "Find the group names"), which this page does not set up.

## Set it up

1. In the Admin Console go to **Applications and Resources > Applications** (**Applications > Applications** in older consoles) and select **Create App Integration**. If asked, choose the classic experience. Pick **OIDC - OpenID Connect** as the sign-in method and **Web Application** as the type. Select **Next**.
2. Name the integration (for example `DFE`). Keep the **Authorization Code** grant type. Under **Sign-in redirect URIs**, put only the redirect URI from the operator. Under **Assignments**, pick **Limit access to selected groups** and choose the groups that use DFE, or **Allow everyone in your organization to access**. Select **Save**.
3. Copy the **Client ID** from the app's settings, and the **Client secret** from its **Client Credentials** section.
4. Add the groups claim to the `default` authorization server, as below.

### Add the groups claim to the `default` server

Go to **Security > API**, open the **Authorization Servers** tab and select `default`.

1. On the **Scopes** tab, look for a scope named `groups`. If there is none, select **Add Scope**, name it `groups`, leave user consent off and create it. DFE asks for this scope, and Okta fails a sign-in that asks for a scope the server does not define.
2. On the **Claims** tab, select **Add Claim**. Set **Name** to `groups`. Set **Include in token type** to **ID Token** and **Always**. Set **Value type** to **Groups**, and **Filter** to **Matches regex** with a pattern for the groups DFE should see, such as `dfe-.*`. `.*` sends every group the user is in. Set **Include in** to **Any scope** and create the claim. With no scope condition the claim arrives whether or not the sign-in asks for `groups`.
3. On the **Access Policies** tab, check that an active policy applies to the DFE app, with an active rule that allows the **Authorization Code** grant for any scopes. If there is none, select **Add Policy**, assign it to the DFE app (or all clients), then **Add Rule** with that grant. The Integrator Free Plan's `default` server ships with no policy.

## Send these to the operator

| Value | Where it comes from |
|---|---|
| Client ID | Step 3 |
| Client secret | Step 3. Send it over a channel fit for a password, never plain email |
| Issuer | `https://<your Okta domain>/oauth2/default` |
| Group names | One per group that should hold a DFE role, with the role it should get. See the next section |

## Find the group names

DFE links an Okta group on its name, exactly as the claim carries it. **Directory > Groups** lists them. Because the link is the name, renaming a group in Okta breaks its link, and a new group given a linked name picks up its DFE roles. Keep the claim filter narrow and limit who can create or rename groups. Once one of the user's groups is linked, the check below also lists the names the claim sends for them.

DFE can instead read groups from the Okta API with an API token. Groups link on their names that way too. The claim above is the normal setup: use the API only if the operator asks for it.

## Check it worked

Once the operator confirms the provider is in place, open this URL in a private browser window and sign in as a user in a mapped group:

```text
https://<your DFE console host>/api/v1/auth/oidc/<provider-name>/login
```

The page answers with JSON. `email` is the user, and `groups` lists the group names the claim sent. The JSON also holds an `access_token`: it is a live DFE session, so do not paste it anywhere. A user in no linked group is refused, and DFE keeps no account for them.

## Common failures

| What you see | Cause | Fix |
|---|---|---|
| Okta page: `The 'redirect_uri' parameter must be a Login redirect URI in the client app settings.` | The redirect URI on the app differs from the one DFE sent | Paste the operator's URI exactly into **Sign-in redirect URIs** |
| DFE page: `OIDC login failed:` ending `User is not assigned to the client application.` | The user is not assigned to the app | Assign the user, or a group they are in |
| DFE page: `OIDC login failed:` ending `One or more scopes are not configured for the authorization server resource.` | The `default` server has no `groups` scope | Add it on the **Scopes** tab, or ask the operator to stop requesting `groups` |
| DFE page: `OIDC login failed:` ending `Policy evaluation failed for this request, please check the policy configurations.` | No active access policy and rule cover the app | Add or activate one on the **Access Policies** tab |
| DFE page: `OIDC login refused: no DFE group with a role is linked to your identity provider groups.` | None of the user's group names is linked yet, or no names arrived: no claim, the claim is set to Userinfo instead of **Always**, the filter matches none of the user's groups, or the issuer is the org server (`https://<your Okta domain>` with no `/oauth2/default`) | Send the operator the names from **Directory > Groups**. If they are linked, recheck the claim on `default` and send the operator the `/oauth2/default` issuer |

## For the DFE operator

Create the provider with `POST /api/v1/auth/oidc-providers`. The `name` becomes `<provider-name>` in the redirect URI, so set it before the admin starts:

```json
{
  "name": "<provider-name>",
  "type": "okta",
  "display_name": "Okta",
  "issuer": "https://<okta domain>/oauth2/default",
  "client_id": "<client id>",
  "client_secret": "<client secret>"
}
```

The default `token_claim` mode reads the `groups` claim, and the default scopes are `openid email profile groups`. To drop the `groups` scope, send `"scopes": ["openid", "email", "profile"]`: a claim set to **Always** and **Any scope** still arrives. The API alternative is `groups.mode: api` with `groups.okta_domain` and `groups.api_token`. The sync and `groups.enrich_on_login: true` both read group names, the same identifiers the claim carries. The client secret goes to the secret store and the provider keeps only its path.

Link each DFE group by setting its `source_id` to a group name the admin sent, and its `source_provider` to `<provider-name>` so no other provider's sign-in can assert the same name: see [rbac.md, section 4.4](../rbac.md#44-group-sync-process). A linked group admits its members only once it carries a role, and until one does no Okta user can sign in, so link the first group as the local admin or the break-glass account. Behind a TLS-terminating proxy, set `api.forwarded_allow_ips` (`DFE_API_FORWARDED_ALLOW_IPS`), or the engine builds the redirect URI with `http://` and Okta refuses it.

## Vendor references

- Create an OIDC app integration: <https://help.okta.com/en-us/content/topics/apps/apps_app_integration_wizard_oidc.htm>
- Authorization servers and API Access Management: <https://developer.okta.com/docs/concepts/auth-servers/>
- Scopes, claims and access policies: <https://developer.okta.com/docs/guides/customize-authz-server/main/>
- Groups claim: <https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/>
- Redirect URI error: <https://support.okta.com/help/s/article/okta-error-400-bad-request-the-redirect-uri-parameter-must-be-a-login-redirect-uri-in-the-client-app-settings>
- Access policy error: <https://support.okta.com/help/s/article/policy-evaluation-failed-for-this-request-when-logging-into-openid-connect-app-via-a-custom-authorization-server>
