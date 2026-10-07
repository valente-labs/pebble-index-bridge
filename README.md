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

The bridge uses one bearer token for Pebble and keeps the separate Grok
webhook key on the server. It does not store or expose conversation text from
status endpoints. Transcription logging is disabled by default.

## Supported contract

- `POST /index` only.
- `multipart/form-data` with exactly `transcription`, `recordedAt`, and
  `client` fields. `client` defaults to `ring` when omitted by a compatible
  sender.
- `Authorization: Bearer <bridge token>` is required on every intake request.
- The queue is durable SQLite at `DB_PATH`, with a persistent volume required
  for container deployments.
- Event identity is stable for normalized transcription, `recordedAt`, and
  `client`. Re-sending the same source event returns the existing receipt.
- Delivery is FIFO. A `needs_attention` event stops later events until an
  operator reconciles it.
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
4. Run the deployment checks and review the generated plan:

   ```bash
   docker compose --env-file .env config --quiet
   docker compose --env-file .env up -d --build --wait
   ```

   For remote SSH deployment, use `scripts/deploy.py apply` with host, root and configuration arguments. Read `python3 scripts/deploy.py --help`
   for the current options.
5. Configure Pebble's webhook to the bridge's HTTPS `/index` URL, add the
   `Authorization` custom header with the bridge token, and select
   **transcription only**. Send a synthetic event first, then a short test
   recording.
6. Use `python3 scripts/queue_admin.py --help` for queue inspection and reconciliation.
   Resolve the head event before retrying later events.

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
