#!/usr/bin/env bash
#  Project:      dfe-engine
#  File:         scripts/gen_issuer_proto.sh
#  Purpose:      Regenerate the vendored issuer (dex) gRPC stubs from api.proto
#  Language:     Bash
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
# Regenerates api_pb2.py / api_pb2_grpc.py / api_pb2.pyi from the vendored
# dex api/v2/api.proto. Run from the repo root after bumping the proto or
# grpcio-tools. The `-I src` root makes the grpc stub's cross-import a proper
# package path (from dfe_engine.auth.issuer.dex.proto import api_pb2), not a
# flat `import api_pb2` that only resolves with the file's own dir on sys.path.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run python -m grpc_tools.protoc \
  -I src \
  --python_out=src \
  --grpc_python_out=src \
  --pyi_out=src \
  src/dfe_engine/auth/issuer/dex/proto/api.proto

echo "Regenerated issuer stubs under src/dfe_engine/auth/issuer/dex/proto/"
