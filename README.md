# OpenRouter CrowdBench

OpenRouter CrowdBench is an independent, community-oriented tool for checking which
OpenRouter model routes respond now—and how reliability, latency, instruction
compliance, empty-output rates, and HTTP 429 rates change over time.

**Principal developer:** Mark Austin

**License:** [MIT](LICENSE)

**Status:** Early public preview; contributions and reproducible observations are welcome.

OpenRouter CrowdBench is not affiliated with or endorsed by OpenRouter.

## What it looks like

### Connect without storing your key

![OpenRouter key connection and security explanation](docs/images/connect-security.png)

### Smoke-test the current model catalog

![CrowdBench model catalog and smoke-test controls](docs/images/smoke-test.png)

### Explore historical routing quality

![CrowdBench historical trends and reliability charts](docs/images/history-trends.png)

## Run it locally

Requirements: Python 3.11 or newer and an OpenRouter API key. The runtime uses only
the Python standard library; `pytest` is needed only for tests.

```bash
git clone https://github.com/maustin10/openrouter-crowdbench.git
cd openrouter-crowdbench
python3 audition.py --port 8012
```

Open <http://127.0.0.1:8012>, paste your OpenRouter key into the connection card,
and choose **Connect securely**. No server-level OpenRouter key is required or read
from `.env`.

To expose the service to other devices on a trusted network:

```bash
python3 audition.py --host 0.0.0.0 --port 8012
```

Do not expose plain HTTP to the public internet. A public deployment must terminate
HTTPS before requests reach CrowdBench.

## Typical workflow

1. Connect a contributor key.
2. Keep **Free models only** selected unless you intentionally want paid routes.
3. Run one fixed smoke probe across all, historically successful, explicitly
   included, or explicitly excluded models.
4. Select the routes that responded and run repeated reliability probes.
5. Use **All-time testing** to compare success, overall score, latency, and 429 rate
   across a selected date window.

The smoke probe is fixed and versioned. A successful response must return expected
visible tokens, which makes observations from different contributors and times more
comparable.

## Contributor-key security

- The key is retained only in the current page's JavaScript memory.
- It is never written to cookies, local storage, session storage, a URL, or request JSON.
- Authenticated calls carry it in the `X-OpenRouter-Key` request header, over HTTPS
  when hosted or directly to the loopback server when running locally.
- The backend passes it directly to the active in-memory worker thread. The key is
  not a property of the job and is released when the worker finishes.
- Jobs, saved reports, the usage ledger, and public history contain no key field.
- The built-in HTTP access log records request paths and status codes, not headers.
- Disconnecting or closing the page removes the browser copy.

For a public deployment, contributors should use a limited, revocable OpenRouter key.
OAuth with PKCE and short-lived keys is the recommended future authentication path.

## Persistence

By default, reports are written to `results/` and request counts to
`usage-ledger.jsonl`. Set `CROWDBENCH_DATA_DIR` to place both under another directory:

```bash
CROWDBENCH_DATA_DIR=/var/data python3 audition.py --host 0.0.0.0 --port 8012
```

These runtime files are excluded from git. Saved reports contain sanitized model and
probe measurements, not contributor credentials. The repository's sanitized starter
history is copied from `seed-results/` only when a new data volume is initialized.
Subsequent contributor reports are written directly to the configured data directory.

## Test it

```bash
python3 -m pip install pytest
python3 -m pytest test_audition.py -q
python3 -m py_compile audition.py
```

The health endpoint can be checked while the service is running:

```bash
curl http://127.0.0.1:8012/healthz
```

## Contributing

Issues and pull requests are welcome. Please keep probes deterministic, avoid adding
credentials or private prompts to fixtures, update tests for behavior changes, and
describe how a metric remains comparable across contributors.

## License

Released under the [MIT License](LICENSE). Mark Austin is the principal developer.
