# Deploying to Amazon Bedrock AgentCore Runtime

Everything in this folder is deployment glue. The container itself is
[`../Dockerfile`](../Dockerfile) → [`../entrypoint.py`](../entrypoint.py) →
`clinic_front_desk.deployment.server`.

## What gets deployed

One ARM64 container on port 8080 serving both agents over the AgentCore Runtime
HTTP protocol contract:

| Path | Method | Serves |
| --- | --- | --- |
| `/ping` | GET | Health. `HealthyBusy` while a call or analysis run is in flight. |
| `/invocations` | POST | Scheduled Practice_Intelligence run, plus role-gated Dashboard BFF reads and decision approve/dismiss. |
| `/ws` | WebSocket | Voice_Front_Desk bidirectional transport (patient audio ↔ Nova Sonic). |

Both agents share one Data_Layer and one change-event channel inside the process,
so a booking made by voice and a Decision approved on the dashboard act on the
same records.

Two AWS resources sit outside the container: the DynamoDB single table, and an
EventBridge schedule that drives the ≤ 24 h analysis cadence.

## Files

| File | Purpose |
| --- | --- |
| `trust-policy.json` | Lets AgentCore Runtime assume the execution role, with confused-deputy guards. |
| `execution-role-policy.json` | Least-privilege permissions: the one DynamoDB table + its GSIs, Nova Sonic invoke, ECR pull, logs/traces. |
| `eventbridge-schedule.json` | EventBridge Scheduler schedule that POSTs `{"action":"run_intelligence"}` every 24 h. |
| `scheduler-invoke-policy.json` | Permissions for the schedule's own role to invoke the runtime. |
| `../scripts/create_table.py` | Provisions the DynamoDB table + GSI1–GSI4. Idempotent. |

Replace `ACCOUNT_ID`, `REGION`, `TABLE_NAME`, and `AGENT_ARN` throughout before
applying. The `Comment` keys in the JSON files are documentation; strip them if
your tooling rejects unknown top-level keys (the AWS CLI accepts them for
`--policy-document` only after removal, so use `jq 'del(.Comment)'`).

## 1. Provision the table

```powershell
python scripts/create_table.py --table clinic-front-desk --region us-east-1
```

## 2. Create the execution role

```powershell
$acct = (aws sts get-caller-identity --query Account --output text)
$region = "us-east-1"

jq "del(.Comment) | walk(if type == \"string\" then gsub(\"ACCOUNT_ID\"; \"$acct\") | gsub(\"REGION\"; \"$region\") else . end)" `
  deploy/trust-policy.json > trust.json
jq "del(.Comment) | walk(if type == \"string\" then gsub(\"ACCOUNT_ID\"; \"$acct\") | gsub(\"REGION\"; \"$region\") | gsub(\"TABLE_NAME\"; \"clinic-front-desk\") else . end)" `
  deploy/execution-role-policy.json > perms.json

aws iam create-role --role-name ClinicFrontDeskAgentCoreRole `
  --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name ClinicFrontDeskAgentCoreRole `
  --policy-name ClinicFrontDeskRuntimeAccess --policy-document file://perms.json
```

Confirm Nova Sonic is actually enabled in the region before deploying:

```powershell
python scripts/check_aws.py
```

## 3. Configure and launch

Install the starter toolkit, then use the checked-in
[`../.bedrock_agentcore.yaml`](../.bedrock_agentcore.yaml) so the deploy is
reproducible rather than dependent on interactive answers:

```powershell
pip install bedrock-agentcore-starter-toolkit

agentcore configure --entrypoint entrypoint.py --name clinic_front_desk `
  --execution-role arn:aws:iam::${acct}:role/ClinicFrontDeskAgentCoreRole `
  --region $region

agentcore launch
agentcore status
```

`agentcore launch` builds the ARM64 image, pushes it to ECR, and creates the
runtime. If you would rather build the image yourself:

```powershell
docker buildx build --platform linux/arm64 -t clinic-front-desk:latest .
```

ARM64 is mandatory — an amd64 image is rejected at deploy time.

## 4. Schedule the intelligence run

Requirement 13.1 caps the analysis interval at 24 h. The in-process
`AnalysisScheduler` enforces that invariant, but the timer that actually fires
in deployment is external:

```powershell
aws scheduler create-schedule --cli-input-json file://deploy/eventbridge-schedule.json
```

## 5. Verify

```powershell
# Health
agentcore invoke --help   # or curl the runtime endpoint directly

# One analysis pass
aws bedrock-agentcore invoke-agent-runtime `
  --agent-runtime-arn $agentArn `
  --payload (echo '{"action":"run_intelligence"}' | base64) `
  out.json; Get-Content out.json

# A role-gated dashboard read (403 without a role — Req 15.7)
aws bedrock-agentcore invoke-agent-runtime `
  --agent-runtime-arn $agentArn `
  --payload (echo '{"action":"open_decisions","role":"doctor"}' | base64) `
  out.json; Get-Content out.json
```

## Local run

```powershell
$env:CLINIC_BACKEND="memory"; python entrypoint.py
# then: GET http://127.0.0.1:8080/ping
```

`CLINIC_BACKEND=memory` swaps the *stores* for in-memory fakes, so `/ping` and
`/invocations` work with no DynamoDB and no credentials. It does **not** stub the
voice layer: `/ws` opens a real Nova Sonic stream, so an actual voice call needs
AWS credentials and Nova Sonic access in the configured region either way. Run
`python scripts/live_voice_smoke.py` for the voice path specifically.

Swap in a real table by dropping `CLINIC_BACKEND` and setting
`CLINIC_TABLE_NAME` / `AWS_REGION`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLINIC_BACKEND` | `dynamodb` | `memory` composes over in-memory fakes (data lost on stop; does not stub Nova Sonic). |
| `CLINIC_TABLE_NAME` | `clinic-front-desk` | DynamoDB single-table name. |
| `AWS_REGION` / `CLINIC_REGION` | `us-east-1` | Region for DynamoDB and Bedrock. |
| `CLINIC_NOVA_SONIC_MODEL_ID` | `amazon.nova-2-sonic-v1:0` | Nova Sonic model bound to the `BidiAgent`. The v2 id is `amazon.nova-2-sonic-v1:0`; v1 is `amazon.nova-sonic-v1:0`. Only v2 supports the turn-detection (barge-in endpointing) config. |
| `CLINIC_ANALYSIS_INTERVAL_HOURS` | `24` | Analysis cadence; clamped to ≤ 24 h (Req 13.1). |
| `CLINIC_CREATE_TABLE_IF_MISSING` | unset | `true` bootstraps the table on start. |
| `CLINIC_DYNAMODB_ENDPOINT_URL` | unset | Endpoint override, e.g. DynamoDB-local. |
| `CLINIC_LOG_LEVEL` | `INFO` | Log level. |
| `CLINIC_RECORDINGS_BUCKET` | unset | S3 bucket for call audio. **Unset means calls are not recorded.** |
| `CLINIC_RECORDINGS_PREFIX` | `call-recordings` | Key prefix inside the bucket. |
| `CLINIC_RECORDINGS_SSE` | `AES256` | Server-side encryption: `AES256` or `aws:kms`. |
| `CLINIC_RECORDINGS_KMS_KEY_ID` | unset | Required when `CLINIC_RECORDINGS_SSE=aws:kms`. |

## Call recordings and transcripts

**Transcripts** are always captured and stored on the `CallSession` in DynamoDB
(capped so they fit inside the 400 KB item limit). **Audio** is only captured when
`CLINIC_RECORDINGS_BUCKET` is set — no bucket, no recording, by design.

Audio is written as one stereo WAV per call (patient left, agent right, laid out on
the real timeline so pauses are preserved):

```
s3://<bucket>/call-recordings/<YYYY>/<MM>/<DD>/<call_session_id>.wav
```

The `CallSession` keeps a `recording_uri` pointing at it. The dashboard serves
`GET /dashboard/calls/{id}` with the transcript plus a short-lived presigned
playback URL, gated on the call-activity view.

### Before you turn recording on

This is patient audio, and recording it carries obligations the code cannot
discharge for you:

- **Consent.** Many jurisdictions require all-party consent to record a call. The
  agent does **not** currently announce that the call is being recorded — if you
  enable recording, add that to the greeting.
- **Retention.** Set an S3 **lifecycle rule** to expire objects under the prefix.
  The date-based key layout exists precisely so one rule can do this; the
  application never deletes audio.
- **Encryption.** The app requests SSE on every write, but also enable **bucket
  default encryption**, ideally with a customer-managed KMS key.
- **Access.** Block public access on the bucket, and remember a presigned URL is a
  bearer capability for its lifetime (default 5 minutes).

Provision the bucket with all of those controls in one step:

```powershell
python scripts/create_recordings_bucket.py `
  --bucket clinic-recordings-<account-id> --region us-east-1 --retention-days 90
```

That blocks all public access, enables default encryption (add `--kms-key-id` for
SSE-KMS), applies a TLS-only bucket policy, and adds the lifecycle rule that expires
recordings under the prefix. It is idempotent, so re-running re-applies the settings.

Versioning is deliberately left **off**. With versioning on, a lifecycle expiration
only writes a delete marker and keeps the audio as a noncurrent version — the
opposite of what a retention policy is for.

Then add `CLINIC_RECORDINGS_BUCKET` to the agent's environment and the
`CallRecordings` statement from `execution-role-policy.json` to its role. The agent
will announce the recording automatically once the bucket is configured.

## Security notes

The container performs **no authentication of its own**. AgentCore Runtime
terminates SigV4 (or OAuth, if you set `authorizer_configuration`) in front of
it, so the container must never be exposed directly to the internet — that is
why `network_mode: PUBLIC` in the toolkit config refers to the *runtime's*
managed endpoint, not to an open container port.

What the container does enforce is **authorization**: every dashboard read and
decision action passes through the `RoleGate`, and a caller with no assigned role
gets `403` with no schedule, activity, or metrics data returned (Req 15.5, 15.7).
The caller's role arrives in the `role` body field or the `X-Clinic-Role` header,
which the authenticating layer in front is responsible for setting from the
verified identity. If you put your own proxy in front, make sure it strips
client-supplied `X-Clinic-Role` headers before forwarding.

Patient data is PHI. The execution-role policy is scoped to the single table, but
you should also enable encryption at rest with a customer-managed KMS key, turn
on point-in-time recovery, and confirm your CloudWatch log retention and
AgentCore observability settings match your compliance requirements. None of that
is configured here.
