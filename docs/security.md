# Security model

This bridge is an authenticated translator that may receive hostile network
traffic. It accepts only a small transcription webhook, stores the event in a
private SQLite database, and makes one configured outbound HTTPS request. The
receiving Grok Bot is a separate security and execution boundary.

## Required controls

- Use a long, unique bridge bearer token and a separate Grok webhook key. Keep
  both in secret files with restrictive permissions. The Grok key never goes
  into Pebble.
- Keep `Authorization: Bearer <bridge token>` on Pebble requests even when the
  endpoint is reachable only over a Tailscale tailnet. Network membership is
  not application identity.
- Use HTTPS for the bridge endpoint and for `GROKBOT_WEBHOOK_URL`. Do not use
  URL userinfo or fragments. Keep the bridge bound to localhost behind the
  chosen HTTPS front end.
- The service never logs raw transcriptions. Logs, metrics, status output, and
  support bundles may contain timestamps, event IDs, counts, and error codes.
  They must never contain raw speech or credentials.
- The database holds every accepted transcription in full, so every backup of
  it holds the transcriptions too. Backups must stay private: mode 0600, a
  private location, encrypted storage, and never attached to a ticket, chat, or
  support bundle. Treat a backup exactly like the live database.
- Mount `/data` as a private persistent volume. The store creates restrictive
  database and directory modes, but host backup and volume permissions remain
  operator responsibilities.

## Event and delivery safety

The public event ID in a receipt is random and reveals nothing about the
content. Duplicate suppression uses a separate private stable hash of the
normalized transcription, `recordedAt`, and `client`. SQLite uniqueness on that
hash makes a repeated submission return the existing receipt (same public ID,
`duplicate: true`) instead of queueing a second copy. The hash stays inside the
database: it is not returned by any endpoint, shown by the operator tool, or
logged, so a leaked ID cannot be used to test guesses about what was said.

`recordedAt` must be exactly 13 ASCII digits. It is forwarded to Grok as a JSON
number. This is local deduplication only. The Grok webhook contract does not
document an idempotency key, duplicate suppression, ordering, or a completion
callback.

The queue therefore uses conservative states:

| State | Meaning | Operator action |
| --- | --- | --- |
| `queued` | Accepted locally and waiting for the worker | None unless it remains blocked |
| `sending` | One worker owns the FIFO head | Investigate after restart; recovery marks it for attention |
| `retry` | A pre-send connection failure may be retried later | Check the next attempt time and network |
| `delivered` | The Grok endpoint returned exactly HTTP 200 | This is webhook acceptance, not task completion |
| `needs_attention` | Outcome is non-200, ambiguous, canceled, or exhausted | Reconcile locally before releasing the FIFO |

An ambiguous timeout must never be treated as proof that no request arrived.
The worker does not resend it automatically. This avoids blind duplicate agent
actions at the cost of requiring operator review. The queue makes no
exactly-once external side-effect claim.

## Audit finding and mitigation matrix

The matrix carries forward the findings from the original upstream bridge
review and records the maintained fork's control or remaining responsibility.

| Finding | Fork mitigation | Remaining responsibility or risk |
| --- | --- | --- |
| Example bearer token could be deployed unchanged | Configuration rejects obvious placeholder values; the example uses a missing secret-file path and an invalid HTTPS URL sentinel | Operators must generate unique secrets and verify startup fails before exposure when sentinels remain |
| Unauthenticated traffic could grow failure state and logs | Failure accounting is bounded; rejection logs aggregate counters; request size and rate limits are bounded | Put an edge or reverse-proxy rate limit in front of any public endpoint; a public service can still be denied service by traffic volume |
| Declared body length was not an intrinsic streamed cap | Intake consumes the ASGI body with a byte cap and rejects conflicting framing; multipart fields and part sizes are bounded. `tests/test_wire.py` checks this over a real loopback uvicorn socket: chunked bodies over the cap, `Content-Length` with `Transfer-Encoding`, repeated framing headers, truncated and malformed multipart, and credentials checked before any body arrives | Keep the tested server/parser configuration and retest after proxy or server changes |
| Excess authenticated requests could create one alert task each | This fork has no per-request outbound security-alert task path; the worker and queue are bounded | External monitoring should detect host or container exhaustion |
| Numeric environment values could fail open | Integer, timeout, URL, host, and secret validation use finite bounds and fail startup on invalid values | Review new settings in code and deployment checks before enabling them |
| Missing downstream URL could start the service | Destination URL and key are required, HTTPS-only, and validated at startup | Operators still control the destination; protect the environment and deployment plan |
| Broad parser catches hid server failures | Expected multipart errors become client errors; unexpected exceptions are not silently relabeled as malformed input | Monitor application errors and dependency changes |
| Malformed non-ASCII authorization could become an internal error | Header decoding and exact bearer comparison fail closed before form parsing | Keep an HTTPS edge that rejects malformed headers consistently |
| Mutable or incomplete build inputs | Pin and review dependency inputs in CI; run the repository security checks before deployment | A base image, package index, or action can drift unless the release process pins and promotes an immutable artifact |
| No durable delivery, replay, or restart recovery | SQLite WAL/FULL sync, append-only event history, persistent `/data`, worker recovery, and operator reconciliation preserve accepted events | A lost database or deleted volume is still data loss; make private backups (which contain transcriptions) and test restore into a new file |
| Timeout or response loss could duplicate a source event | The private stable hash deduplicates repeated Pebble submissions and the same receipt is returned; ambiguous outcomes become `needs_attention` and are not auto-resubmitted | Grok may have accepted an ambiguous request. Human reconciliation is required |
| Concurrent forwarding could reorder utterances | One worker claims one FIFO head at a time; an attention head blocks later events | Grok routine execution and specialist work can still complete asynchronously after acceptance |
| Shutdown could kill an in-flight delivery | `sending` is recovered as `needs_attention` after restart; deployment should allow the configured request timeout to drain | Never infer failure from a missing local response; inspect the owning routine before retrying |
| Provider status contract was unclear | The Grok integration treats only HTTP 200 as acceptance and records the status | Grok documents acceptance, not completion. A future provider change requires an explicit contract update |
| Health check could overstate readiness | `/health` reports store and worker availability; it does not claim Grok task completion | Treat delivery counts and attention state as separate operational signals |
| Narrow multipart contract could drift with Pebble | Docs pin the baseline to transcription-only `transcription`, `recordedAt`, and `client` fields and reject files/unknown fields | Pebble app versions can change. Re-test the documented payload before enabling new modes |

## Stored data and capacity

Accepted events and their history are never deleted by the bridge. The record
and byte limits count delivered events too, so a long-running deployment can
reach `MAX_RECORDS` or `MAX_BYTES`. When any limit is reached, intake answers
HTTP 503 and logs a capacity event; nothing already accepted is removed or
overwritten. This favors keeping accepted speech over accepting new speech. Operator status reports the oldest unfinished
head event and how much of each capacity limit is in use, so a stuck head or a
filling store is visible before intake stops. The maintenance steps are in
[`deployment.md`](deployment.md#capacity-maintenance). No step deletes data
automatically.

## Bearer identity risk

The bridge token authenticates possession, not a person, device, or Pebble
account. Anyone who obtains it can submit a transcription-shaped instruction.
The Grok webhook key has the same bearer property for the downstream routine.
Tailscale ACLs, HTTPS, secret files, least-privilege Bot tools, and rotation
reduce exposure but do not create cryptographic sender identity. Rotate both
tokens after suspected disclosure, review the owning Bot's permissions, and
reconcile any event whose delivery outcome is uncertain.

## Network boundary

Tailscale Serve can provide a private HTTPS path for an operator-controlled
tailnet. Tailscale Funnel makes a service reachable from the public internet
and must be an explicit operator decision. This project enables neither
automatically. A private route does not remove the bearer check. Pebble's
public webhook documentation specifies HTTPS and custom headers, but does not
promise that every cloud delivery path can reach a tailnet-only hostname, so
validate private Serve from the actual sender before relying on it.

## What the bridge cannot attest

An HTTP 200 from Grok says the webhook was accepted or a run started. The
bridge cannot attest that the transcription appeared in the owning Bot's
conversation, that memory was updated, that a specialist was delegated work,
that an approval was granted, or that any local or cloud task finished. Those
behaviors belong to the Grok Bot routine and its cloud computer configuration.

## References

- [Grok Bot routine webhook behavior](https://prod.cursor.com/help/grok-bot/routines)
- [Grok Bot routines and automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Grok Bot approvals, security, and privacy](https://docs.x.ai/grok-bot/approvals-security-and-privacy)
- [Pebble Index webhook fields](https://help.repebble.com/en/articles/15724406-index-advanced-features-mcp-webhook)
- [Tailscale tailnets](https://tailscale.com/docs/concepts/tailnet)
- [Tailscale Serve](https://tailscale.com/docs/use-cases/application-testing/share-local-dev-server-with-team)
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)
