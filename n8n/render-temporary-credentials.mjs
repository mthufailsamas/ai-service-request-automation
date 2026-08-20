import { readFileSync, writeFileSync } from "node:fs";

const webhookToken = process.env.N8N_WORKFLOW_START_TOKEN;
const primaryToken = process.env.PRIMARY_WORKFLOW_TOKEN;

if (!webhookToken || webhookToken.length < 32) {
  throw new Error("N8N_WORKFLOW_START_TOKEN must contain at least 32 characters");
}
if (!primaryToken || primaryToken.length < 32) {
  throw new Error("PRIMARY_WORKFLOW_TOKEN must contain at least 32 characters");
}

const credentials = JSON.parse(
  readFileSync("/setup/workflow-start-credentials.json", "utf8"),
);
const webhookCredential = credentials.find(
  (credential) => credential.id === "workflow-start-webhook-auth-v1",
);
const primaryCredential = credentials.find(
  (credential) => credential.id === "primary-workflow-api-auth-v1",
);

if (!webhookCredential || !primaryCredential) {
  throw new Error("The workflow-start credential template is incomplete");
}

webhookCredential.data.value = `Bearer ${webhookToken}`;
primaryCredential.data.token = primaryToken;

writeFileSync(
  "/tmp/workflow-start-credentials.json",
  JSON.stringify(credentials),
  { encoding: "utf8", mode: 0o600 },
);
