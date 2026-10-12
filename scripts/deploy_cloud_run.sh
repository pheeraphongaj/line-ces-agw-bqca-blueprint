#!/usr/bin/env bash
# ==============================================================================
# File: scripts/deploy_cloud_run.sh
# Description:
#   Automated deployment script for:
#     1) Partner Service Accounts & IAM Impersonation bindings
#     2) Internal Employee Cloud IAP Auth Service (`line-iap-auth`)
#     3) LINE <-> CES <-> GEAP AGW <-> BQCA Adapter (`line-bqca-adapter`)
#     4) Runtime URL resolution (`gcloud run services describe --format=value(status.url)`)
# ==============================================================================
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID in .env or environment}"
: "${GCP_PROJECT_NUMBER:?Set GCP_PROJECT_NUMBER in .env or environment}"
: "${GCP_REGION:=us-central1}"
: "${BQ_DATA_PROJECT_ID:=$GCP_PROJECT_ID}"
: "${CES_LOCATION:=us}"
: "${CES_APP_ID:?Set CES_APP_ID in .env or environment}"
: "${BQCA_LOCATION:=us}"
: "${BQCA_AGENT_ID:?Set BQCA_AGENT_ID in .env or environment}"
: "${CHART_BUCKET:=${GCP_PROJECT_ID}-line-charts}"
: "${LOGIN_RICHMENU_ID:=}"
: "${UNLOCKED_RICHMENU_ID:=}"

HOST_SA="${GCP_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "==> [1/5] Ensuring Partner Service Accounts & IAM Impersonation..."
for SA_NAME in "sa-partner-polk" "sa-partner-east"; do
  SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${SA_NAME}" \
      --display-name="BigQuery Native RLS Partner SA (${SA_NAME})" \
      --project="${GCP_PROJECT_ID}"
  fi

  # Grant Host Cloud Run SA permission to mint short-lived RAM tokens for Partner SA
  gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${GCP_PROJECT_ID}" \
    --member="serviceAccount:${HOST_SA}" \
    --role="roles/iam.serviceAccountTokenCreator" \
    --quiet

  # Grant BQCA & BigQuery Job execution on Host Project
  for ROLE in "roles/geminidataanalytics.dataAgentUser" "roles/geminidataanalytics.dataAgentStatelessUser" "roles/bigquery.jobUser"; do
    gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="${ROLE}" \
      --condition=None \
      --quiet
  done

  # CRITICAL GUARDRAIL: Grant ONLY roles/bigquery.dataViewer on Data Project
  # NEVER grant roles/bigquery.filteredDataViewer at Project/Dataset level!
  gcloud projects add-iam-policy-binding "${BQ_DATA_PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/bigquery.dataViewer" \
    --condition=None \
    --quiet
done

echo "==> [2/5] Deploying Internal Employee Cloud IAP Gate (line-iap-auth)..."
gcloud run deploy line-iap-auth \
  --source="./services/line-iap-auth" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --no-allow-unauthenticated \
  --iap \
  --set-env-vars="GCP_PROJECT_ID=${GCP_PROJECT_ID},GCP_PROJECT_NUMBER=${GCP_PROJECT_NUMBER},GCP_REGION=${GCP_REGION},UNLOCKED_RICHMENU_ID=${UNLOCKED_RICHMENU_ID},LOGIN_RICHMENU_ID=${LOGIN_RICHMENU_ID}" \
  --set-secrets="LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest" \
  --quiet

IAP_AUTH_URL="$(gcloud run services describe line-iap-auth --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" --format='value(status.url)')"
echo "    Resolved IAP_AUTH_URL=${IAP_AUTH_URL}"

echo "==> [3/5] Deploying LINE <-> CES <-> AGW <-> BQCA Adapter (line-bqca-adapter)..."
gcloud run deploy line-bqca-adapter \
  --source="./services/line-bqca-adapter" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --allow-unauthenticated \
  --set-env-vars="GCP_PROJECT_ID=${GCP_PROJECT_ID},CES_LOCATION=${CES_LOCATION},CES_APP_ID=${CES_APP_ID},BQCA_LOCATION=${BQCA_LOCATION},BQCA_AGENT_ID=${BQCA_AGENT_ID},CHART_BUCKET=${CHART_BUCKET},IAP_AUTH_BASE_URL=${IAP_AUTH_URL},UNLOCKED_RICHMENU_ID=${UNLOCKED_RICHMENU_ID},LOGIN_RICHMENU_ID=${LOGIN_RICHMENU_ID},LINE_REPLY_CUTOFF_SECONDS=22.0" \
  --set-secrets="LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest" \
  --quiet

ADAPTER_URL="$(gcloud run services describe line-bqca-adapter --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" --format='value(status.url)')"
echo "    Resolved ADAPTER_URL=${ADAPTER_URL}"

echo "==> [4/5] Publishing Runtime Endpoint Configuration to gs://${CHART_BUCKET}/config/runtime.json..."
cat <<EOF > /tmp/runtime_config.json
{
  "ces_app_id": "${CES_APP_ID}",
  "public_base_url": "${ADAPTER_URL}",
  "iap_auth_base_url": "${IAP_AUTH_URL}",
  "unlocked_richmenu_id": "${UNLOCKED_RICHMENU_ID}",
  "login_richmenu_id": "${LOGIN_RICHMENU_ID}"
}
EOF
gcloud storage cp /tmp/runtime_config.json "gs://${CHART_BUCKET}/config/runtime.json" --project="${GCP_PROJECT_ID}"
rm -f /tmp/runtime_config.json

echo "==> [5/5] Deployment Complete!"
echo "    LINE Webhook URL : ${ADAPTER_URL}/line/webhook"
echo "    Partner Portal   : ${ADAPTER_URL}/auth/portal"
echo "    FastMCP Endpoint : ${ADAPTER_URL}/mcp/"
echo "    Internal IAP URL : ${IAP_AUTH_URL}/auth/login"
