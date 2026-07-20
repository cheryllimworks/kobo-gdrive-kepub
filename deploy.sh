#!/usr/bin/env bash
# Run this from inside this folder (where main.py, requirements.txt, and
# kepubify live) after filling in the variables below.
#

set -euo pipefail

# ---- Fill these in first ----
PROJECT_ID="your-project-id"
REGION="us-central1"
INBOX_FOLDER_ID="paste-inbox-folder-id-here"
CONVERTED_FOLDER_ID="paste-converted-folder-id-here"
FAILED_FOLDER_ID="paste-failed-folder-id-here"
# ------------------------------

SERVICE_ACCOUNT_NAME="kobo-drive-bot"
INVOKER_ACCOUNT_NAME="kobo-scheduler-invoker"
FUNCTION_NAME="kobo-convert"

gcloud config set project "$PROJECT_ID"

echo "== Enabling required APIs =="
gcloud services enable \
  drive.googleapis.com \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com

echo "== Granting the Cloud Build service accounts permission to build =="
# On some accounts (particularly ones managed under a Google Workspace/Cloud
# Identity organisation) Google's automatic grant of this permission gets
# blocked by an org policy, and the deploy step below fails with "missing
# permission on the build service account". Running this up front avoids
# hitting that blind; it's a no-op if the permission was already there.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" || true
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" || true

echo "== Creating the service account the function runs as (no key file, ever) =="
gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
  --display-name="Kobo Drive Bot" || echo "(already exists, continuing)"

SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "Service account: $SERVICE_ACCOUNT_EMAIL"
echo ">>> Share your 3 Drive folders (inbox/converted/failed) with this address as Editor before continuing. <<<"
read -p "Press enter once you've done that... "

echo "== Deploying the Cloud Function =="
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=process_inbox \
  --trigger-http \
  --no-allow-unauthenticated \
  --service-account="$SERVICE_ACCOUNT_EMAIL" \
  --memory=512Mi \
  --timeout=120s \
  --set-env-vars="INBOX_FOLDER_ID=${INBOX_FOLDER_ID},CONVERTED_FOLDER_ID=${CONVERTED_FOLDER_ID},FAILED_FOLDER_ID=${FAILED_FOLDER_ID}"

FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" --region="$REGION" --gen2 --format='value(serviceConfig.uri)')
echo "Function URL: $FUNCTION_URL"

echo "== Creating a separate identity for Cloud Scheduler to call the function with =="
gcloud iam service-accounts create "$INVOKER_ACCOUNT_NAME" \
  --display-name="Kobo Scheduler Invoker" || echo "(already exists, continuing)"

INVOKER_EMAIL="${INVOKER_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud functions add-invoker-policy-binding "$FUNCTION_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${INVOKER_EMAIL}"

echo "== Creating the schedule (every 15 minutes) =="
gcloud scheduler jobs create http kobo-convert-job \
  --schedule="*/15 * * * *" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$INVOKER_EMAIL" \
  --oidc-token-audience="$FUNCTION_URL" \
  --location="$REGION"

echo ""
echo "Done."
echo "Service account you shared your Drive folders with: $SERVICE_ACCOUNT_EMAIL"
echo "Function URL (for manual test runs): $FUNCTION_URL"
echo "Remember: Billing → Budgets & alerts → set a small budget (e.g. \$1) on this project."
