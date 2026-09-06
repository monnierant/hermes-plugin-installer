#!/bin/sh
set -eu

HERMES_BIN=${HERMES_BIN:-/home/hermes/.hermes/hermes-agent/venv/bin/hermes}
PYTHON_BIN=${PYTHON_BIN:-/home/hermes/.hermes/hermes-agent/venv/bin/python}
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

SOURCE_REPO="$TMP_ROOT/source"
HERMES_HOME="$TMP_ROOT/home"
export HERMES_HOME
mkdir -p "$SOURCE_REPO" "$HERMES_HOME/plugins/plugin_installer"
cp "$PROJECT_DIR/tests/fixtures/integration_plugin/plugin.yaml" \
   "$PROJECT_DIR/tests/fixtures/integration_plugin/__init__.py" \
   "$SOURCE_REPO/"
cp "$PROJECT_DIR/plugin.yaml" "$PROJECT_DIR/__init__.py" \
   "$PROJECT_DIR/cli.py" "$PROJECT_DIR/core.py" "$PROJECT_DIR/README.md" \
   "$HERMES_HOME/plugins/plugin_installer/"

git -C "$SOURCE_REPO" init -q
git -C "$SOURCE_REPO" add plugin.yaml __init__.py
git -C "$SOURCE_REPO" \
  -c user.name='Hermes Test' \
  -c user.email='test.invalid@example.invalid' \
  commit -q -m 'test: fixture'
COMMIT_SHA=$(git -C "$SOURCE_REPO" rev-parse HEAD)

"$HERMES_BIN" plugins enable plugin_installer >/dev/null
"$HERMES_BIN" plugin-installer install "$SOURCE_REPO" \
  --consent "INSTALL file://$SOURCE_REPO@$COMMIT_SHA"
"$HERMES_BIN" plugins doctor installer_integration_fixture --ci
"$HERMES_BIN" plugins list --json > "$TMP_ROOT/list.json"
"$PYTHON_BIN" -m json.tool "$TMP_ROOT/list.json" >/dev/null
test -d "$HERMES_HOME/plugins/installer_integration_fixture"
