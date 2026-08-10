# Tinza Marketplace — Production Setup Guide

Automated multi-store product upload pipeline: n8n orchestrates the workflow, a Flask API on Cloud Run proxies WooCommerce credentials, Supabase holds routing/state data, and Google Drive triggers uploads per store.

---

## Table of Contents
1. [Prerequisites](#1-prerequisites)
2. [Google Cloud Infrastructure](#2-google-cloud-infrastructure)
3. [Credentials & External Keys](#3-credentials--external-keys)
4. [Supabase Setup](#4-supabase-setup)
5. [Deploy the Flask API](#5-deploy-the-flask-api)
6. [Deploy n8n](#6-deploy-n8n)
7. [Post-Deployment](#7-post-deployment)

---

## 1. Prerequisites

- An active GCP project with billing enabled
- Generate the app-level encryption keys locally and keep them somewhere safe until Step 5:
  ```bash
  # AES key for WooCommerce credential encryption
  openssl rand -hex 32

  # n8n encryption key
  openssl rand -hex 16
  ```

---

## 2. Google Cloud Infrastructure

### 2.1 Create the project and enable APIs
1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project.
2. Enable these APIs under **APIs & Services → Enable APIs**:
   - Google Drive API
   - Cloud Run API
   - Cloud Build API
   - Artifact Registry API

### 2.2 Create the service account
This account is shared by n8n and Flask to access Google Drive.

1. Go to **IAM & Admin → Service Accounts → Create Service Account**.
2. Gant it the **Editor** role.
3. Open the account → **Keys** tab → **Add Key → Create new key** → JSON → download.
4. Rename the downloaded file to exactly `service-account.json` and place it in the root of your Flask project.
5. Copy the service account's email address — you'll use it to create a **Google Service Account** credential in n8n.

---

## 3. Credentials & External Keys

### 3.1 WordPress/WooCommerce (per vendor store)
1. Log in to the WordPress dashboard as an administrator.
2. Go to **WooCommerce → Settings → Advanced → REST API**.
3. Click **Add Key**, set permissions to **Read/Write**, and click **Generate API Key**.
4. Copy the **Consumer key** and **Consumer secret** immediately — the secret is shown only once.

### 3.2 Groq API key
1. Sign up at the [Groq Cloud Console](https://console.groq.com).
2. Go to **API Keys → Create API Key**, name it, and submit.
3. Copy the generated key immediately.

---

## 4. Supabase Setup

Create the project


### 4.1 Run the schema
```sql
CREATE SCHEMA n8n_system;
CREATE EXTENSION "pgcrypto";

CREATE TABLE stores (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  store_name VARCHAR NOT NULL,
  wc_user TEXT NOT NULL,
  wc_pass TEXT NOT NULL,
  drive_folder_id VARCHAR NOT NULL,
  default_status VARCHAR DEFAULT 'draft'
);

CREATE TABLE webhook_channels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id UUID REFERENCES stores(id) ON DELETE CASCADE,
  channel_id VARCHAR NOT NULL,
  resource_id VARCHAR NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  renewed_at TIMESTAMP,
  webhook_url VARCHAR
);

CREATE TABLE processing_queue (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  store_id UUID NOT NULL,
  folder_id TEXT NOT NULL,
  folder_name TEXT NOT NULL,
  drive_folder_id TEXT NOT NULL,
  default_status TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  retry_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT processing_queue_store_folder_uniq UNIQUE (store_id, folder_id)
);

CREATE INDEX idx_stores_folder ON stores(drive_folder_id);
CREATE INDEX idx_channels_store ON webhook_channels(store_id);
CREATE INDEX idx_channels_channel ON webhook_channels(channel_id);
CREATE INDEX idx_processing_queue_status_created ON processing_queue(status, created_at ASC);

-- Only one active job per store/folder at a time
CREATE UNIQUE INDEX idx_queue_active_folder
ON processing_queue (store_id, folder_id)
WHERE status IN ('pending', 'processing');

-- Auto-purge old completed/failed jobs every 5 hours
CREATE EXTENSION pg_cron;

SELECT cron.schedule(
  'purge-completed-queue-jobs',
  '0 */5 * * *',
  $$
    DELETE FROM public.processing_queue
    WHERE status IN ('done', 'failed')
      AND updated_at < NOW() - INTERVAL '3 days';
  $$
);
```

### 4.2 Create the storage bucket
1. In the Supabase dashboard, go to **Storage → New bucket**.
2. Toggle **Public bucket** ON.
3. Copy your **Project URL** and **service_role secret key** for the `.env`.

### 4.3 Get database connection details for `.env`

Use the **Session Pooler** connection method for configuring .env.

| `.env` variable | Where to find it |
|---|---|
| `DB_HOST` | "Host" |
| `DB_PORT` | "Port" (usually `5432`) |
| `DB_NAME` | "Database name" (usually `postgres`) |
| `DB_USER` | "User" |
| `DB_PASS` | Password you set when creating the project (use **Reset password** if forgotten) |

---

## 5. Deploy the Flask API

Authenticate first:
```bash
gcloud auth login
PROJECT_ID=$(gcloud config get-value project)
REGION=us-central1
```

### 5.1 Store the service account key in Secret Manager
```bash
gcloud services enable secretmanager.googleapis.com

# Create the secret container
gcloud secrets create google-sa-key --replication-policy="automatic"

# From the folder containing service-account.json:
gcloud secrets versions add google-sa-key --data-file="service-account.json"
```

### 5.2 Grant IAM roles
Get your project number:
```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
```

Grant the Cloud Run default service account the roles it needs:
```bash
# Read the service account key from Secret Manager
gcloud secrets add-iam-policy-binding google-sa-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Read uploaded source code during build
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

# Write build logs
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/logging.logWriter"

# Build container images
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder"
```

### 5.3 Deploy
```bash
gcloud run deploy tinza-flask-api \
  --source . \
  --region $REGION \
  --port=8080 \
  --cpu=1 \
  --memory=512Mi \
  --min-instances=1 \
  --env-vars-file .env \
  --set-secrets="/secrets/sa-key.json=google-sa-key:latest" \
  --set-env-vars GCS_BUCKET_NAME=your-actual-bucket-name \
  --allow-unauthenticated
```

Copy the resulting service URL — you'll enter it (plus the relevant `/path`) into the n8n nodes that call this API.

---

## 6. Deploy n8n

### 6.1 Create the Cloud SQL instance
Takes 5–10 minutes:
```bash
gcloud sql instances create tinza-n8n-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=$REGION \
  --project=$PROJECT_ID
```

### 6.2 Create the database and user
```bash
gcloud sql databases create n8n \
  --instance=tinza-n8n-db \
  --project=$PROJECT_ID

gcloud sql users create n8n_user \
  --instance=tinza-n8n-db \
  --password="<your-cloudsql-password>" \
  --project=$PROJECT_ID
```

### 6.3 Create secrets
```bash
echo -n "<your-cloudsql-password>" | gcloud secrets create n8n-cloudsql-password --data-file=-
echo -n "<your-n8n-encryption-key>" | gcloud secrets create n8n-encryption-key --data-file=-
```

### 6.4 Grant IAM roles
```bash
gcloud secrets add-iam-policy-binding n8n-cloudsql-password \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding n8n-encryption-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

### 6.5 Deploy
```bash
gcloud run deploy tinza-n8n \
  --image=n8nio/n8n:2.6.4 \
  --region=$REGION \
  --port=5678 \
  --cpu=1 \
  --memory=2Gi \
  --no-cpu-throttling \
  --concurrency=1 \
  --min-instances=1 \
  --max-instances=1 \
  --timeout=600 \
  --add-cloudsql-instances=$PROJECT_ID:$REGION:tinza-n8n-db \
  --set-env-vars="N8N_PORT=5678,\
DB_TYPE=postgresdb,\
DB_POSTGRESDB_HOST=/cloudsql/$PROJECT_ID:$REGION:tinza-n8n-db,\
DB_POSTGRESDB_PORT=5432,\
DB_POSTGRESDB_DATABASE=n8n,\
DB_POSTGRESDB_USER=n8n_user,\
DB_POSTGRESDB_SCHEMA=public,\
DB_POSTGRESDB_SSL_ENABLED=false,\
DB_POSTGRESDB_CONNECTION_LIMIT=5,\
DB_POSTGRESDB_POOL_SIZE=5,\
GENERIC_TIMEZONE=Africa/Casablanca,\
TZ=Africa/Casablanca,\
N8N_RUNNERS_ENABLED=false,\
N8N_PROXY_HOPS=1" \
  --set-secrets="DB_POSTGRESDB_PASSWORD=n8n-cloudsql-password:latest,\
N8N_ENCRYPTION_KEY=n8n-encryption-key:latest" \
  --allow-unauthenticated
```


### 6.6 Set the final host URL
Once deployed, grab the service URL and re-run to register it with n8n itself:
```bash
gcloud run services update tinza-n8n \
  --region=$REGION \
  --update-env-vars="\
N8N_HOST=<your-n8n-service-url>,\
N8N_PROTOCOL=https,\
N8N_EDITOR_BASE_URL=https://<your-n8n-service-url>,\
WEBHOOK_URL=https://<your-n8n-service-url>/"
```

---

## 7. Post-Deployment

- **Enable Two-Factor Authentication** on the n8n instance: install an authenticator app (e.g. Authenticator), scan the QR code, and enter the generated code.
- Use the n8n service URL in the **Register Drive Webhook** node and the **Encryption API** call node.
- Use the Flask service URL (+ endpoint path) in every n8n node that calls the Flask API.
