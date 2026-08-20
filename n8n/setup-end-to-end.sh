#!/bin/sh
set -eu

credentials_file=/tmp/workflow-start-credentials.json
trap 'rm -f "$credentials_file"' EXIT

node /setup/render-temporary-credentials.mjs
n8n import:credentials --input="$credentials_file"
n8n import:workflow --input=/setup/workflow-start-v1.json
n8n import:workflow --input=/setup/human-decision-resume-v1.json
workflow_start_id=$(node -p "require('/setup/workflow-start-v1.json').id")
human_resume_id=$(node -p "require('/setup/human-decision-resume-v1.json').id")
test -n "$workflow_start_id"
test -n "$human_resume_id"
test "$workflow_start_id" != "$human_resume_id"
n8n publish:workflow --id="$workflow_start_id"
n8n publish:workflow --id="$human_resume_id"
