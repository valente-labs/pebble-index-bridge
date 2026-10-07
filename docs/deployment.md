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

The bridge is generic HTTPS software and does not depend on Tailscale. Tailscale
Serve is one optional way to provide the HTTPS route. It exposes the local
listener over a private tailnet. Put the bridge host and the actual Pebble
delivery path in a tailnet that can reach the Serve endpoint, configure ACLs
for the smallest required source set, and verify the route with a synthetic
request. The deployment helper touches Tailscale only when you pass the
explicit `--serve-port` flag, and then it configures only that port.

Keep the application bearer token required. Tailscale membership is a network
condition, not the bridge's application identity. If the Pebble cloud delivery
path cannot reach a tailnet-only hostname, use a generic public HTTPS endpoint
with strong bearer authentication and edge rate limiting instead.

Tailscale Funnel is public internet exposure. This fork does not enable Funnel,
reset Funnel state, or assume a Funnel hostname. If an operator chooses a
public route, apply an edge rate limit and monitor the host independently.

## Container deployment checks

There are two ways to run the container. Use one consistently, because the
Compose project name decides which containers and volumes later commands touch.

### Local Docker host

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build --wait
docker compose --env-file .env exec index-bridge python scripts/queue_admin.py status
```

This uses the default Compose project name for the checkout directory and the
named `index-bridge-data` volume.

### Remote host over SSH with `scripts/deploy.py`

The first argument is the action: `check`, `plan`, or `apply`. These flags are
required for every action: `--ssh-host`, `--remote-root`, and `--config`.
`--serve-port` is optional.

```bash
python3 scripts/deploy.py check --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json
python3 scripts/deploy.py plan  --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json
python3 scripts/deploy.py apply --ssh-host HOST --remote-root /srv/index-bridge --config deployment.json
```

`check` validates the remote prerequisites (`docker`, `docker-compose`) and
the configuration, `plan` prints the revision and image that would be
deployed, and `apply` builds the image on the host, starts it, waits for
`/health`, and keeps `REMOTE_ROOT/data` in place. The tree must be committed
because the helper deploys `git archive` of `HEAD`. The private JSON config
contains exactly `ALLOWED_HOSTS`, `INDEX_BRIDGE_SUBNET`, and
`INDEX_BRIDGE_ADDRESS`. Provision the three mode-0600 secret files under
`REMOTE_ROOT/secrets` through your encrypted secret manager before applying.
Choose an unused private subnet and a usable static address.

To use Tailscale Serve, add `--serve-port PORT` (1024 to 65535) to all three
commands so the port-ownership check also runs in `check` and `plan`. Leave it
out for any other HTTPS ingress. The helper never enables Funnel.

On the remote host the helper runs Compose with the project name `pebble-grok`
and the file `deploy/tower-compose.yaml` from the current release directory.
Run operator commands the same way, not with a bare `docker compose`, which
would address a different (empty) project:

```bash
REMOTE_ROOT=/srv/index-bridge
RELEASE=$(cat "$REMOTE_ROOT/current-release")
bridge() {
  docker-compose --project-name pebble-grok --env-file "$RELEASE/.env" \
    -f "$RELEASE/deploy/tower-compose.yaml" "$@"
}
bridge ps
bridge exec index-bridge python scripts/queue_admin.py status
```

The examples below use `bridge exec index-bridge ...`. On a local Docker host,
substitute `docker compose --env-file .env exec index-bridge ...`.

### Operator actions

`queue_admin.py` runs inside the container, has no network interface, and
takes `status`, `backup DESTINATION`, or `resolve EVENT_ID --outcome ...`.

For a head event that needs review:

```bash
bridge exec index-bridge python scripts/queue_admin.py resolve <event-id> --outcome accepted
bridge exec index-bridge python scripts/queue_admin.py resolve <event-id> --outcome retry --acknowledge-duplicate-risk
```

Use `accepted` only after checking the owning routine's run history. Use
`retry` only when the operator accepts the possibility that Grok already
received the event. The operator tool must not blindly resend an event whose
network outcome is ambiguous.

`status` prints metadata only: counts by state, the oldest unfinished (head)
event with its state and age, capacity use against `MAX_PENDING`,
`MAX_RECORDS`, and `MAX_BYTES`, and recent events with error codes. It never
prints transcription text, so its output is safe to share.

The container baseline should retain a non-root user, read-only root
filesystem, dropped Linux capabilities, `no-new-privileges`, bounded CPU and
memory, and no Docker socket or broad host mounts. A writable persistent `/data`
volume is the deliberate exception for SQLite state.

## Backup and restore

The database holds every accepted transcription. A backup is a full copy of
that content. Keep backups at mode 0600 in a private, encrypted, access
controlled location, and never put one in a support bundle, ticket, or chat.

### Backup

```bash
bridge exec index-bridge python scripts/queue_admin.py backup /data/backup-YYYYMMDD.sqlite3
```

The destination must not already exist; the tool refuses to overwrite a file.
It uses SQLite's online backup, so the copy is consistent while the service
runs, and creates it at mode 0600. The container's root filesystem is
read-only, so write the file under `/data` (that is `REMOTE_ROOT/data` on a
remote host). Move it to private backup storage promptly and check that the
copy opens before relying on it.

### Restore into a new file

Restore never overwrites a live database. It creates a new database file and
leaves the old one in place until the new one is checked.

1. Stop the service (`bridge stop`, or `docker compose stop`) so nothing is
   writing.
2. Rename, do not delete, the current data directory, for example
   `mv data data.before-restore-YYYYMMDD`. This keeps the old database and its
   `-wal` and `-shm` companions together.
3. Create a new `data` directory (mode 0700, owner UID 10001), then copy the
   backup into it as a new file named `index-bridge.sqlite3` with mode 0600.
4. Start the service and run `queue_admin.py status`. Compare the counts and
   the head event with what you expect.
5. Keep the renamed directory until you are satisfied. Removing it is a
   separate, deliberate decision.

A backup is a point in time. Events delivered after it was taken come back as
`queued` after a restore, so the worker will send them to Grok again once the
service starts, and an event that was `sending` becomes `needs_attention`.
`queue_admin.py resolve` only handles `needs_attention` events, so there is no
operator action that marks a restored `queued` event as already delivered.
Restore from the most recent backup you have, expect possible duplicate Bot
runs for events accepted after it, and compare against the routine history
afterwards. If duplicate runs are not acceptable, do not restore an old backup
over a database that is still readable; copy it for inspection instead.

## Capacity maintenance

The bridge never deletes accepted events or their history, and no setting
makes it do so. When `MAX_PENDING`, `MAX_RECORDS`, or `MAX_BYTES` is reached,
new intake is refused with HTTP 503 and everything already stored stays as it
is. Delivered events still count toward `MAX_RECORDS` and `MAX_BYTES`, so a
healthy deployment eventually reaches those two limits. Check `status` for
the oldest head event and capacity use before that happens.

When capacity is tight:

1. Run `status`. If the head is `needs_attention` or `retry`, it is blocking
   delivery and driving `MAX_PENDING` up. Reconcile it first (see above).
2. Take a private backup (see above) before changing anything.
3. If the limit is `MAX_RECORDS` or `MAX_BYTES`, raise it in the configuration
   within the validated range (`MAX_PENDING` up to 100000, `MAX_RECORDS` up to
   10000000, `MAX_BYTES` up to 1073741824) and restart. Check free disk space
   on the data volume first. The container memory limit is small, so raise
   limits in steps.
4. Alternatively, retire the full database: stop the service, rename the data
   directory (do not delete it), create a fresh private `data` directory, and
   start. The old directory remains as private history. Duplicate suppression
   does not span the two databases, so an old event submitted again would be
   accepted as new.

Do not delete rows by hand, delete the database file, or truncate the
volume to make room. Any pruning of old delivered history is an offline
operator decision made after a verified backup; the bridge does not provide
or perform it.

## Pebble webhook configuration

In the Pebble app's Index webhook settings:

1. Set the HTTPS URL to the bridge's `/index` route.
2. Add the custom header `Authorization` with value `Bearer <bridge token>`.
3. Enable transcription delivery and choose transcription only.
4. Keep audio upload disabled for this integration.
5. Confirm the request contains the three baseline fields: `transcription`,
   `recordedAt` as Unix milliseconds, and `client` as `ring`.

`recordedAt` must be exactly 13 digits. The bridge rejects anything else and
forwards the value to Grok as a JSON number.

Pebble documents webhook delivery as HTTPS `multipart/form-data` with arbitrary
headers. The bridge intentionally rejects audio files and unknown fields so
the durable contract remains small and auditable.

## Reconciliation and failure handling

Use the operator tool to inspect counts, the oldest head event, and recent
metadata. The status API and operator output must not be used as a transcript
viewer. When the head event is
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
