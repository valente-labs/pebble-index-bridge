# Pebble transcription to Grok Bot bridge

Valente Labs maintains this small self-hosted translator for one narrow path:

```text
Pebble webhook (multipart/form-data)
        -> authenticated bridge
        -> durable SQLite queue
        -> Grok Bot routine webhook (application/json)
```

Pebble sends a transcription-only multipart request. The bridge validates the
three supported fields, stores the event before acknowledging Pebble, and
forwards this JSON shape to the configured Grok Bot routine:

```json
{
  "source": "pebble-index-01",
  "transcription": "Example spoken text.",
  "recordedAt": 1790037251116,
  "client": "ring",
  "bridgeRequestId": "<stable-event-id>"
}
```

The bridge uses one bearer token for the Ring caller (Pebble) and keeps the
separate Grok webhook key on the server. The Grok key never goes to Pebble.

## Privacy

Every accepted transcription is stored in full in the SQLite database so it
can be delivered, reconciled, and kept as history. The database file and
every backup of it therefore contain the transcriptions. They must stay
private: restrictive file modes, a private volume, encrypted and access
controlled backup storage, and never attached to a ticket, chat, or support
bundle.

Status endpoints, the operator `status` output, logs, metrics, and support
bundles carry only metadata such as event IDs, states, counts, timestamps, and
error codes. They never carry raw speech or credentials. Share those, not the
database or a backup, when asking for help.

## Supported contract

- `POST /index` only.
- `multipart/form-data` with exactly `transcription`, `recordedAt`, and
  `client` fields. `client` defaults to `ring` when omitted by a compatible
  sender.
- `Authorization: Bearer <bridge token>` is required on every intake request.
- The queue is durable SQLite at `DB_PATH`, with a persistent volume required
  for container deployments.
- `recordedAt` must be exactly 13 ASCII digits (Unix milliseconds). The
  bridge forwards it to Grok as a JSON number, not a string.
- The public event ID in a receipt is random and carries no content. A
  separate private hash of the normalized transcription, `recordedAt`, and
  `client` decides whether an event is a duplicate. Re-sending the same source
  event returns the same receipt with `duplicate: true` and does not queue a
  second copy. The private hash is never returned or logged.
- Intake answers HTTP `202` only after the event is durable in SQLite.
- Delivery is FIFO. A `needs_attention` event stops later events until an
  operator reconciles it.
- Capacity limits (`MAX_PENDING`, `MAX_RECORDS`, `MAX_BYTES`) make intake
  answer HTTP `503` when reached. The bridge never deletes an accepted event or
  its history to make room. Delivered events still count toward the record and
  byte limits. See the capacity section of
  [`docs/deployment.md`](docs/deployment.md).
- A connection failure before a request is sent may receive bounded retry.
  Timeouts, cancellations, process restarts, and other ambiguous outcomes do
  not auto-resend because the downstream may already have accepted the body.
- For the Grok Bot routine integration, only HTTP `200` is an acceptance
  signal. It means the routine run was accepted or started. It does not mean
  that the routine completed, posted a message, or finished downstream work.

There is no exactly-once external delivery guarantee. A stable local event ID
prevents duplicate intake, but the Grok webhook contract does not publish an
idempotency guarantee or a completion callback.

## Quick start

1. Create or edit a webhook-triggered routine on the owning Grok Bot from the
   desktop routine UI. Copy its live `POST to`, key, and header values. The
   routine instruction should tell the Bot to treat the JSON `transcription`
   value as the spoken request and post it visibly in the owning Bot
   conversation before doing any specialist delegation it is configured to do.
2. Copy `.env.example` to `.env`. Put `BRIDGE_TOKEN` and
   `GROKBOT_WEBHOOK_KEY` in files referenced by `BRIDGE_TOKEN_FILE` and
   `GROKBOT_WEBHOOK_KEY_FILE`; do not place secret values in Git or shell
   history. Store the URL in `GROKBOT_WEBHOOK_URL_FILE` as well.
3. Give the service a persistent private directory mounted at `/data` and set
   `DB_PATH=/data/index-bridge.sqlite3`.
4. Validate and start the service on the local Docker host:

   ```bash
   docker compose --env-file .env config --quiet
   docker compose --env-file .env up -d --build --wait
   ```

   For a remote host over SSH, `scripts/deploy.py` takes the action
   (`check`, `plan`, or `apply`) as a positional argument and requires
   `--ssh-host`, `--remote-root`, and `--config`. Run `check`, then `plan`,
   then `apply`. That helper uses the Compose project name `pebble-grok`, so
   later operator commands on that host must use the same project. See
   [`docs/deployment.md`](docs/deployment.md).
5. Configure Pebble's webhook to the bridge's HTTPS `/index` URL, add the
   `Authorization` custom header with the bridge token, and select
   **transcription only**. Send a synthetic event first, then a short test
   recording.
6. Use `scripts/queue_admin.py` inside the container for `status`, `backup`,
   and `resolve`. It has no remote interface. Resolve the head event before
   retrying later events. Exact commands for both deployment styles are in
   [`docs/deployment.md`](docs/deployment.md).

The complete generic deployment and private-network guidance is in
[`docs/deployment.md`](docs/deployment.md). The control and audit matrix is in
[`docs/security.md`](docs/security.md).

## Grok Bot boundary

The bridge owns intake, durable local state, and the outbound HTTP request. It
does not own Grok Bot conversation history, routine memory, approvals, cloud
computer access, or Bot-to-Bot specialist delegation. A webhook trigger starts
a routine run according to Grok's current Bot configuration. It is not a
continuation token for the HTTP request, and the bridge cannot infer when a
routine or specialist has finished.

Grok's cloud computer and a user's local computer are separate execution
boundaries. Tailscale can provide an optional private network path to the
bridge, but it does not change where Grok runs and does not replace bearer
authentication.

## Network choices

The bridge listens locally. Put a generic HTTPS reverse proxy, managed HTTPS
endpoint, or equivalent translator in front of it. Tailscale Serve is an
optional private alternative when the Pebble delivery path can reach the
tailnet endpoint. Configure the Serve route explicitly with the deployment helper or your own ingress and test
it from the actual Pebble path. Tailscale Funnel is public exposure and is
never enabled automatically by this project. Even on a private tailnet, keep
the bridge bearer token and HTTPS validation enabled.

## Attribution

This maintained fork is by Valente Labs and preserves attribution to the
original [`Zhigre/pebble-index-bridge`](https://github.com/Zhigre/pebble-index-bridge)
project and its upstream MIT notice.
The fork adds durable intake, stable deduplication, FIFO delivery, operator
reconciliation, and deployment hardening. See [`LICENSE`](LICENSE) for the
license text present in this checkout.

## References

- [Grok Bot routines](https://prod.cursor.com/help/grok-bot/routines)
- [xAI routines and automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Pebble Index webhooks](https://help.repebble.com/en/articles/15724406-index-advanced-features-mcp-webhook)
- [Tailscale Serve](https://tailscale.com/docs/use-cases/application-testing/share-local-dev-server-with-team)
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)
