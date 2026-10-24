# agros-hfh: voice outreach (M0)

Fixture-first outbound voice outreach service for a kidney-care education pilot. A
coordinator dashboard queues calls to contacts. A deterministic script engine runs each
call, and a classifier (rules, or LangGraph on AWS Bedrock) labels each participant reply
with one allowed intent. The app records structured outcomes, handoffs, and a
hash-chained audit log.

> **Status: demo only.** The script in `src/agros_voice/scripts/` is synthetic. Henry
> Ford Health, an IRB, and clinical leadership have not reviewed or approved it. Live
> calls, real contacts, recording, and transcript retention are all off by default. See
> [docs/security/voice-enrollment-data-flow.md](docs/security/voice-enrollment-data-flow.md).

## Architecture

```
Coordinator -> CloudFront (HTTPS) -> ALB -> api (FastAPI + dashboard)
                                             |  Postgres (RDS, RLS per tenant)
worker  <- jobs (SKIP LOCKED) ---------------+
  |-- fixture provider: simulates the participant, posts signed webhooks to the api
  `-- livekit provider: creates a LiveKit room + agent dispatch
agent (LiveKit Agents): dials via LiveKit SIP -> Twilio -> phone
  Deepgram STT -> classify (Bedrock/LangGraph or rules) -> script engine -> Cartesia TTS
  reports intents only (no text or audio) via signed webhooks
```

| Module | Purpose |
| --- | --- |
| `script.py` | Deterministic state machine (`start` / `advance` / `replay`). All speech comes from the script. |
| `classify.py` | Maps an utterance to one allowed intent. Distress and opt-out are always detected by rules. If the LLM fails, it falls back to rules. |
| `services.py` | Domain operations. Tenant-scoped (`with_tenant`), audited, idempotent. |
| `webhooks.py` | HMAC-SHA256 signature over `timestamp.body`, with a 5-minute replay window. |
| `audit.py` | The only writer for `audit_events`. Per-tenant SHA-256 hash chain, redacted payloads. |
| `providers/` | `fixture` (network-free personas) and `livekit` (SIP dial-out). |
| `agent/livekit_agent.py` | Live voice worker. |
| `infra/` | AWS CDK: VPC, RDS, ECS Fargate (api / worker / agent), ALB, CloudFront, Secrets Manager, Bedrock IAM. |

Webhook events are `call.started`, `turn.completed {seq,node,intent}`, `call.failed` and
`call.ended {turn_count}`. The server stores events idempotently by `event_id` and
recomputes the dialogue from all stored intents, so duplicates and out-of-order delivery
give the same result. Events that don't map to exactly one attempt are quarantined.
Events that arrive after an attempt is terminal are ignored.

## Local development

```sh
uv sync --all-extras
docker run -d --name agros-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16
uv run pytest -q                      # creates and migrates the agros_test database
uv run ruff check src tests && uv run mypy src

# run the app
docker exec agros-pg psql -U postgres -c 'create database agros'
export ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/agros
export APP_DB_PASSWORD=local_app_password DATABASE_URL=postgresql://agros_app:local_app_password@localhost:55432/agros
export SEED_PASSWORD=Local-pass-123 VOICE_AGENT_ENABLED=true
uv run agros-voice bootstrap          # migrate + seed demo tenant, users and fixture contacts
uv run agros-voice api &              # http://localhost:8000  (admin@agros-demo.test / $SEED_PASSWORD)
uv run agros-voice worker
```

Seeded roles are `admin@`, `admin+manager@`, `admin+cta@` and `admin+viewer@agros-demo.test`.
A second tenant (`other+admin@...`) is seeded to check tenant isolation.

## Deploying to AWS

```sh
cd infra && uv venv .venv && uv pip install -p .venv -r requirements.txt && source .venv/bin/activate
npm i -g aws-cdk@2.170.0
cdk bootstrap && cdk deploy                       # fixture mode
# live voice (after filling the VendorSecrets secret and creating the LiveKit SIP trunk):
cdk deploy -c liveVoice=true -c agentDesiredCount=1 -c liveCallAllowlist=+1XXXXXXXXXX -c classifierMode=bedrock
```

The seeded user password is stored in the `SeedPasswordSecretArn` output.

## Safety gates (env)

| Variable | Default | Effect |
| --- | --- | --- |
| `VOICE_AGENT_ENABLED` | `false` | No calls can be queued |
| `VOICE_AGENT_PROVIDER` | `fixture` | `livekit` places real calls |
| `VOICE_LIVE_CALLS_APPROVED` | `false` | Required for `livekit` |
| `VOICE_LIVE_CALL_ALLOWLIST` | empty | Only these E.164 numbers can be dialed live |
| `VOICE_REAL_CONTACTS_APPROVED` | `false` | Imports are limited to fictional 555-01xx or allowlisted numbers |
| `VOICE_CALL_RECORDING_ENABLED` / `VOICE_TRANSCRIPT_RETENTION_ENABLED` | `false` | No code path stores audio or text |
