#!/bin/sh
set -eu

credentials_file=/tmp/workflow-start-credentials.json
trap 'rm -f "$credentials_file"' EXIT

node /setup/render-temporary-credentials.mjs
n8n import:credentials --input="$credentials_file"
n8n import:workflow --input=/setup/recovery-v1.json
n8n publish:workflow --id=recoveryworkflowv1
