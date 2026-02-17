// packages/api/src/middleware/oidc-connection.ts
//
// Spike: OIDC claim-to-ClickHouse connection mapping middleware for HyperDX.
//
// Maps oauth2-proxy identity headers (X-Forwarded-Groups, X-Forwarded-Email)
// to a HyperDX Connection object, automatically selecting the correct
// ClickHouse user and permissions for each proxied request.
//
// Supports:
//   - Entra ID (groups as GUIDs)
//   - Okta (groups as display names)
//   - Google (email-based mapping, no groups claim)
//
// Slots into the existing middleware chain in api-app.ts:
//   app.use('/clickhouse-proxy', isUserAuthenticated, resolveOidcConnection, clickhouseProxyRouter);
//
// Configuration via environment variables:
//   OIDC_GROUP_CONNECTION_MAP  - JSON: group identifier → Connection name
//   OIDC_EMAIL_CONNECTION_MAP  - JSON: email or @domain → Connection name
//   OIDC_DEFAULT_CONNECTION    - Connection name fallback

import type { NextFunction, Request, Response } from 'express';

import Connection from '@/models/connection';
import logger from '@/utils/logger';

// ---------------------------------------------------------------------------
// Configuration (parsed once at startup)
// ---------------------------------------------------------------------------

type ClaimMap = Record<string, string>;

function parseEnvMap(envVarName: string): ClaimMap {
  const raw = process.env[envVarName];
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch (e) {
    logger.error(`Invalid JSON in ${envVarName}: ${e}`);
    return {};
  }
}

const groupMap = parseEnvMap('OIDC_GROUP_CONNECTION_MAP');
const emailMap = parseEnvMap('OIDC_EMAIL_CONNECTION_MAP');
const defaultConnection = process.env.OIDC_DEFAULT_CONNECTION ?? '';

const isEnabled =
  Object.keys(groupMap).length > 0 ||
  Object.keys(emailMap).length > 0 ||
  defaultConnection !== '';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getHeader(req: Request, name: string): string | undefined {
  const val = req.headers[name];
  if (!val) return undefined;
  return Array.isArray(val) ? val[0] : val;
}

/**
 * Match a list of group identifiers against the group map.
 * Groups can be GUIDs (Entra ID) or display names (Okta).
 * First match wins — configure map from most-privileged to least.
 */
function matchGroup(groups: string[]): string | undefined {
  for (const group of groups) {
    const connectionName = groupMap[group];
    if (connectionName) return connectionName;
  }
  return undefined;
}

/**
 * Match an email address against the email map.
 * Supports exact match ("admin@example.com") and domain suffix ("@example.com").
 * Exact match takes precedence over domain suffix.
 */
function matchEmail(email: string): string | undefined {
  // Exact match first
  if (emailMap[email]) return emailMap[email];

  // Domain suffix match
  const atIndex = email.indexOf('@');
  if (atIndex !== -1) {
    const domain = email.substring(atIndex); // "@example.com"
    if (emailMap[domain]) return emailMap[domain];
  }

  return undefined;
}

// ---------------------------------------------------------------------------
// Middleware
// ---------------------------------------------------------------------------

/**
 * Express middleware that resolves OIDC identity claims to a HyperDX Connection.
 *
 * Resolution order:
 *   1. Skip if x-hyperdx-connection-id already set (explicit UI selection)
 *   2. Match X-Forwarded-Groups against OIDC_GROUP_CONNECTION_MAP
 *   3. Match X-Forwarded-Email against OIDC_EMAIL_CONNECTION_MAP
 *   4. Use OIDC_DEFAULT_CONNECTION as fallback
 *   5. Pass through (no injection, existing HyperDX behaviour)
 *
 * When a match is found, the middleware injects x-hyperdx-connection-id
 * into the request headers. The existing getConnection middleware then
 * resolves it to ClickHouse credentials as normal.
 */
export async function resolveOidcConnection(
  req: Request,
  _res: Response,
  next: NextFunction,
) {
  // Don't override explicit connection selection from the UI
  if (req.headers['x-hyperdx-connection-id']) {
    return next();
  }

  // No mapping configured — skip entirely (feature disabled)
  if (!isEnabled) {
    return next();
  }

  const teamId = req.user?.team;
  if (!teamId) {
    return next();
  }

  let connectionName: string | undefined;

  // 1. Try group match (Entra ID GUIDs, Okta display names)
  const groupsHeader = getHeader(req, 'x-forwarded-groups');
  if (groupsHeader && Object.keys(groupMap).length > 0) {
    const groups = groupsHeader.split(',').map(g => g.trim());
    connectionName = matchGroup(groups);
  }

  // 2. Try email match (Google, or fallback for any provider)
  if (!connectionName) {
    const email = getHeader(req, 'x-forwarded-email');
    if (email && Object.keys(emailMap).length > 0) {
      connectionName = matchEmail(email);
    }
  }

  // 3. Try default connection
  if (!connectionName && defaultConnection) {
    connectionName = defaultConnection;
  }

  // No match — pass through to existing behaviour
  if (!connectionName) {
    return next();
  }

  // Resolve connection name to MongoDB _id
  const connection = await Connection.findOne({
    name: connectionName,
    team: teamId,
  });

  if (connection) {
    req.headers['x-hyperdx-connection-id'] = connection._id.toString();
    logger.debug(`OIDC claim resolved to connection "${connectionName}"`);
  } else {
    logger.warn(
      `OIDC mapped to connection "${connectionName}" but not found in team`,
    );
  }

  next();
}
