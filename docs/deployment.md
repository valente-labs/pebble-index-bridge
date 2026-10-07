# Generic deployment

This fork is designed to run as a small self-hosted service. The examples use
generic names and paths so the same instructions work on a private server,
cloud VM, or an always-on Tower host. Do not copy live tokens into commands,
Git, tickets, or screenshots.

## Prerequisites

- A Pebble Index device and the Pebble mobile app.
- A Grok Bot with a webhook-triggered routine owned by the Bot that should
  receive the transcription.
- Docker Engine and Compose, or an equivalent container runtime.
- A persistent private volume mounted as `/data` for `DB_PATH`.
- An HTTPS route to the bridge. This may be a generic reverse proxy or an
  optional Tailscale Serve route.

## Grok routine setup

Use the current desktop Grok Bot routine UI to create or edit the routine. Copy
the live `POST to`, key, and header values from the routine panel. Do not invent
the URL shape or key prefix and do not put the key in Pebble.

The routine instruction should explicitly say that its JSON `transcription`
field is the spoken request. If the desired user experience is a visible
message in the owning Bot conversation, instruct the routine to post the
transcription there before any specialist delegation. Grok's routine run and
its specialist work remain cloud-side behavior outside this bridge.

The webhook contract currently used here is:

```text
POST <copied routine URL>
Authorization: Bearer <copied routine key>
Content-Type: application/json
```

The bridge treats only HTTP 200 as accepted. Grok's documentation describes
that response as acceptance or a started run. It does not publish a response
body, completion callback, idempotency guarantee, or exactly-once guarantee.

## Configure secrets and storage

```bash
cp .env.example .env
chmod 600 .env
mkdir -p secrets data
chmod 700 secrets data
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > secrets/bridge-token.txt
```

Write the copied Grok key to `secrets/grok-webhook-key.txt`, set both secret-file
paths in `.env`, and write the exact HTTPS URL copied from the routine to
`secrets/grok-webhook-url.txt`. All three host files must be readable by UID 10001
and mode 0600. On Linux, run `sudo chown 10001:10001 secrets/*.txt data`. Keep `DB_PATH=/data/index-bridge.sqlite3`.
The deployment must mount `data` at `/data` and must make it writable by the
container runtime user. The application creates the SQLite database with
restrictive file permissions.

Do not set both `BRIDGE_TOKEN` and `BRIDGE_TOKEN_FILE`, or both
`GROKBOT_WEBHOOK_KEY` and `GROKBOT_WEBHOOK_KEY_FILE`. The file form is the
documented default.

## Generic HTTPS route

Keep the application bound to its local listener and terminate HTTPS at the
reverse proxy or managed endpoint. Forward only `POST /index` to the service;
the bridge itself still authenticates the bearer header. Preserve the request
body and `multipart/form-data` boundary. Do not add a proxy that follows or
rewrites the Grok destination.

Before adding Pebble, send a synthetic request through the same HTTPS route:

```bash
curl -i -X POST https://bridge.example.invalid/index \
  -H 'Authorization: Bearer <bridge-token>' \
  -F 'transcription=synthetic deployment check' \
  -F 'recordedAt=1790037251116' \
  -F 'client=ring'
```

The bridge should return a local queue receipt with HTTP 202. That receipt
means durable intake, not Grok completion. Check the operator status view for
the delivery state.

## Optional private Tailscale Serve

Tailscale Serve can expose the local listener over a private tailnet HTTPS
route. Put the bridge host and the actual Pebble delivery path in a tailnet
that can reach the Serve endpoint, configure ACLs for the smallest required
source set, and verify the route with a synthetic request. Tailscale Serve is
operator-managed. The deployment helper configures only the selected port when
`--serve-port` is supplied; test support for the selected target on your installation.

Keep the application bearer token required. Tailscale membership is a network
condition, not the bridge's application identity. If the Pebble cloud delivery
path cannot reach a tailnet-only hostname, use a generic public HTTPS endpoint
with strong bearer authentication and edge rate limiting instead.

Tailscale Funnel is public internet exposure. This fork does not enable Funnel,
reset Funnel state, or assume a Funnel hostname. If an operator chooses a
public route, apply an edge rate limit and monitor the host independently.

## Container deployment checks

The repository's root scripts are the operator interface:

```bash
python3 scripts/deploy.py check --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json
python3 scripts/deploy.py plan --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json
python3 scripts/deploy.py apply --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json --serve-port 8449
docker compose exec index-bridge python scripts/queue_admin.py status
```

`check` validates prerequisites and configuration, `plan` prints the
intended changes, and `apply` performs the selected deployment changes.
The private JSON configuration has exactly `ALLOWED_HOSTS`, `INDEX_BRIDGE_SUBNET`,
and `INDEX_BRIDGE_ADDRESS`. Provision the three mode-0600 secret files under
`REMOTE_ROOT/secrets` through your encrypted secret manager before applying.
Choose an unused private subnet and a usable static address. Omit `--serve-port`
for deployments using another HTTPS ingress. Generic local Docker deployment
uses `docker compose --env-file .env up -d --build --wait`.
Review the plan before applying it. Read each script's `--help` output for
current options. The operator tool reconciles queue state; it should not
blindly resend an event whose network outcome is ambiguous.

For a head event that needs review, the operator flow is explicit:

```bash
docker compose exec index-bridge python scripts/queue_admin.py resolve <event-id> --outcome accepted
docker compose exec index-bridge python scripts/queue_admin.py resolve <event-id> --outcome retry --acknowledge-duplicate-risk
```

Use `accepted` only after checking the owning routine's run history. Use
`retry` only when the operator accepts the possibility that Grok already
received the event. The status output contains metadata and error codes, not
transcription text.

The container baseline should retain a non-root user, read-only root
filesystem, dropped Linux capabilities, `no-new-privileges`, bounded CPU and
memory, and no Docker socket or broad host mounts. A writable persistent `/data`
volume is the deliberate exception for SQLite state.

## Pebble webhook configuration

In the Pebble app's Index webhook settings:

1. Set the HTTPS URL to the bridge's `/index` route.
2. Add the custom header `Authorization` with value `Bearer <bridge token>`.
3. Enable transcription delivery and choose transcription only.
4. Keep audio upload disabled for this integration.
5. Confirm the request contains the three baseline fields: `transcription`,
   `recordedAt` as Unix milliseconds, and `client` as `ring`.

Pebble documents webhook delivery as HTTPS `multipart/form-data` with arbitrary
headers. The bridge intentionally rejects audio files and unknown fields so
the durable contract remains small and auditable.

## Reconciliation and failure handling

Use the operator tool to inspect counts and recent metadata. The status API and
operator output must not be used as a transcript viewer. When the head event is
`needs_attention`, inspect the owning Grok Bot routine conversation and its run
history before choosing a local action. A 200 response may mean the run was
accepted even if the bridge did not persist that response, so a retry can
create a duplicate Bot run.

The queue stops at the first attention event to preserve FIFO order. Resolve or
explicitly account for that event before releasing later entries. A process
restart converts an in-flight `sending` row to `needs_attention`; it does not
claim that the event failed or that it is safe to resend.

## Cloud and local boundaries

Grok routines run in Grok's cloud Bot environment according to the owning Bot's
permissions and network setup. The bridge's local machine only receives the
Pebble webhook, stores its queue, and sends the configured HTTPS request. It
does not receive a Grok completion callback and cannot know whether a delegated
specialist or local computer task finished. Configure approvals, Bot memory,
conversation continuity, and specialist routing in Grok separately.

## Official references

- [Grok Bot routines](https://prod.cursor.com/help/grok-bot/routines)
- [xAI routines and automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Grok Bot work and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)
- [Grok Bot private networks](https://docs.x.ai/grok-bot/private-networks)
- [Grok Bot computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)
- [Pebble Index MCP and webhooks](https://help.repebble.com/en/articles/15724406-index-advanced-features-mcp-webhook)
- [Tailscale tailnets](https://tailscale.com/docs/concepts/tailnet)
- [Tailscale Serve](https://tailscale.com/docs/use-cases/application-testing/share-local-dev-server-with-team)
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)
