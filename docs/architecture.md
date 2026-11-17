# Architecture and tradeoffs

## Runtime

One Python process owns a collection worker and a threaded HTTP server. The scheduler submits blocking HTTP calls to a bounded `ThreadPoolExecutor`. Discovery and result checks each occupy at most one slot. Event polling can occupy at most eight slots, with one in-flight request per event.

Each thread reuses its own `requests.Session`. A lock protects budget admission so simultaneous calls reserve their request rows before network I/O. The rolling local budget survives process restarts because admission checks read the request journal. The collector also reads the provider's `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` response headers. These counters can differ because the provider's reset window differs from the local trailing-hour window.

## Timing

| Observed game state | Polling policy |
| --- | --- |
| Before Q4 | Low-frequency discovery only |
| Early Q4 | Event polls up to 20 seconds apart |
| Approaching 3:34 | Approximately 1.5 seconds between starts |
| From 3:30 | Approximately one second between starts |
| 3:26–3:14 inclusive | Record the first usable decision and continue observation |
| First snapshot below 3:14 | Stop detailed event polling |
| Disappeared game or Q4 0:00 | Batch result lookups, at most once per two minutes per event |

Intervals describe requested start times, not guarantees. HTTP latency, provider throttling, thread capacity, and stalled clocks affect the actual cadence. Every successful event response becomes an observation. Repeated data remains visible because it measures the upstream feed's behavior.

## Persistence

SQLite tables serve distinct audit purposes.

| Table | Stores |
| --- | --- |
| `events` | Latest score and period, discovery timestamps, final score |
| `observations` | Time-stamped Q4 snapshots with captured line and price |
| `decisions` | One immutable selection decision per event and its settlement |
| `calls` | Request start time, endpoint, HTTP status and success flag |
| `meta` | Trial start, duration, state, and provider quota metadata |

WAL mode permits concurrent readers while the collector commits writes. Connections close after each transaction. The database belongs in local application storage because cloud-evicted WAL companion files caused disk I/O errors during the exploratory trial. A worker retry loop tolerates transient collection errors and a dashboard reconnect state exposes availability failures.

## Presentation

The browser fetches `/api/state` after the previous refresh completes. Refreshing the page does not trigger provider requests. All table values are rendered from persisted observations. Provider names pass through HTML escaping. Export endpoints supply events, observations, decisions, and calls as CSV. Decision exports include separate `picked` and `line_result` fields so hypothetical results do not inflate the selected paper record.

The demo generates fictional scores and prices and feeds them through the same persistence, signal, and settlement functions. It creates no provider requests. Demo and live data directories are separate by default.

## Boundaries and next work

- Run one collector per database. The in-process admission lock does not coordinate multiple collector processes.
- The supported adapter targets the Bet365 GG League basketball schema. Feed schema changes require new fixtures and parser validation.
- The 24-hour deadline stops collection. A settlement-only grace period would resolve games still pending at that boundary.
- Source snapshots do not carry an independently verified market timestamp. An unchanged clock can reflect a pause or delayed source data.
- The equity chart uses the most recent 200 decision rows, while summary totals query the entire trial.
- The local HTTP server is suited to a desktop evaluation. Shared hosting would require authentication, deployment hardening, and an external process supervisor.
