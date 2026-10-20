# Email Triage Agent

An AI agent that reads incoming emails, decides what kind of email each one is, and writes a reply **draft**
for a person to check and send.

It sorts emails into seven categories: **incident**, **bank statement**, **financial disbursement**,
**payment request**, **customer complaint**, **KYC document** and **phishing**. Emails that fit none of them
are ignored.

Key points:

- **Safe by default.** It never sends email unless you turn that on. Replies are saved as drafts.
- **Guardrails around the AI.** Every email is treated as untrusted. Prompt-injection attempts, spoofed
  senders and made-up facts in replies are caught by plain code checks, not by trusting the AI.
- **Works with any mailbox.** Gmail, Outlook, Yahoo or any IMAP server. A built-in **mock inbox**
  (23 sample emails, including 6 attacks) lets you try it without a real mailbox.
- **Observable.** OpenTelemetry traces, metrics and logs, with a ready-made Grafana dashboard.

**Tech stack:** Python 3.12, Pydantic, Gemini or Hugging Face (plain HTTP), OpenTelemetry, Docker Compose.

How it works inside, with diagrams: **[ARCHITECTURE.md](ARCHITECTURE.md)**.
How secrets are handled: **[SECURITY.md](SECURITY.md)**.

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Quick start: local Python](#2-quick-start-local-python)
3. [Quick start: Docker](#3-quick-start-docker)
4. [Where secrets go](#4-where-secrets-go)
5. [Connect a real mailbox](#5-connect-a-real-mailbox)
6. [Sending replies (optional)](#6-sending-replies-optional)
7. [Run it all the time (deploy)](#7-run-it-all-the-time-deploy)
8. [Dashboard (Grafana)](#8-dashboard-grafana)
9. [Running the tests](#9-running-the-tests)
10. [Output files](#10-output-files)
11. [Command-line options](#11-command-line-options)
12. [Project layout](#12-project-layout)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Before you start

You need:

| What | Why | Where to get it |
|---|---|---|
| **Python 3.12+** *or* **Docker Desktop** | To run the agent | [python.org](https://www.python.org/downloads/) / [docker.com](https://www.docker.com/products/docker-desktop/) |
| **A Gemini API key** (free) | The AI model | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| A mailbox login *(optional)* | Only to try a real inbox | See [section 5](#5-connect-a-real-mailbox) |

> **Free Gemini keys have a small daily limit** (about 20 requests a day on `gemini-2.5-flash`).
> The agent makes about **3 requests per email**, and many emails (attacks, auto-replies) need none.
> For a first try, use `--limit 5`. If you hit the limit, see [Troubleshooting](#13-troubleshooting).

You do **not** need an API key to run the tests. They are fully offline.

---

## 2. Quick start: local Python

**Step 1 - Get the code and install**

```bash
git clone <this-repo-url>
cd ltfs-demo
python -m venv .venv
```

Activate the virtual environment:

```bash
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows (PowerShell or cmd)
```

Then install:

```bash
pip install -r requirements-dev.txt
```

**Step 2 - Check that everything works (no key needed)**

```bash
pytest
```

You should see all tests pass.

**Step 3 - Add your Gemini key**

```bash
cp .env.example .env             # Windows PowerShell: Copy-Item .env.example .env
```

Open `.env` and set `GEMINI_API_KEY=your-key`. The `.env` file is in `.gitignore`, so it is never committed.

**Step 4 - Run on the mock inbox**

```bash
python -m email_agent.main --source mock --limit 5 --save-drafts
```

At the end you will see a table like this:

```
ID       STATUS        CATEGORY                  CONF  FLAGS
m01      escalated     incident                  0.95
m03      filed         bank_statement            0.98
m05      drafted       financial_disbursement    0.90
...
Accuracy vs expected status: 5/5
```

The reply drafts are in `output/drafts/*.eml` (open them with any mail app or text editor).
The full results are in `output/results.jsonl`.

To run the same emails again, add `--fresh`. Without it, emails that were already handled are skipped.

---

## 3. Quick start: Docker

Docker runs the agent **and** the dashboard (Grafana) with one command.

**Step 1 - Create the secret files**

In Docker, secrets are **files** in the `secrets/` folder, not lines in `.env`
([why?](#4-where-secrets-go)). Docker needs all four files to exist. Leave the ones you don't use empty.

Linux / macOS:

```bash
printf '%s' 'your-gemini-key' > secrets/gemini_api_key
touch secrets/imap_password secrets/imap_oauth_token secrets/hf_token
```

Windows PowerShell:

```powershell
Set-Content -NoNewline -Path secrets\gemini_api_key -Value 'your-gemini-key'
'imap_password', 'imap_oauth_token', 'hf_token' | ForEach-Object {
    if (-not (Test-Path "secrets\$_")) { New-Item -ItemType File "secrets\$_" | Out-Null } }
```

The `secrets/` folder is in `.gitignore` and `.dockerignore`, so these files never go to GitHub or into an image.

**Step 2 - Optional settings**

```bash
cp .env.example .env             # then remove GEMINI_API_KEY etc. from it - Docker uses secrets/ instead
```

This step is optional for the mock run. If a secret still arrives as an environment variable,
the agent logs a warning.

**Step 3 - Run**

```bash
docker compose run --rm tests                                            # offline tests
docker compose up -d grafana otel-collector                              # dashboard
docker compose run --rm email-agent --source mock --limit 5 --save-drafts
```

Open the dashboard at **http://localhost:3000** (login `admin` / `password`). See [section 8](#8-dashboard-grafana).

To stop everything: `docker compose down`.

**Linux only:** the container runs as user ID 10001. Let it read the secrets and write the output:

```bash
mkdir -p output && sudo chown 10001 output
chmod 400 secrets/* && sudo chown 10001 secrets/*
```

---

## 4. Where secrets go

| How you run it | Put secrets in | Why |
|---|---|---|
| Local Python | `.env` | Simple. `.env` is never committed. |
| Docker | `secrets/<name>` files | Docker mounts them read-only at `/run/secrets/`. Environment variables can be read by anyone with `docker inspect`; secret files cannot. |

The four secrets are `gemini_api_key`, `hf_token`, `imap_password` and `imap_oauth_token`.

Other safety rules in the code:

- Secrets are stored as Pydantic `SecretStr`, so they print as `**********` in logs and errors.
- They are never written to traces, logs or output files.
- `config/*.env` (the provider profiles) hold **no** secrets. They are built into the Docker image.
- Locally, you can also use the `secrets/` files instead of `.env`. Set `SECRETS_DIR=secrets` in your
  shell before running.

More detail: [SECURITY.md](SECURITY.md).

---

## 5. Connect a real mailbox

The agent connects to any IMAP server over TLS (port 993) and always checks the server's certificate.
Each provider has a small settings file in `config/` (server names and ports only, no secrets):

| Provider | Flag | What you need |
|---|---|---|
| Gmail / Google Workspace | `--profile gmail` | `IMAP_USERNAME` (your address) and an [App Password](https://myaccount.google.com/apppasswords) in `IMAP_PASSWORD`. Needs 2-Step Verification. |
| Yahoo | `--profile yahoo` | `IMAP_USERNAME` and an app password in `IMAP_PASSWORD` |
| Outlook / Microsoft 365 | `--profile outlook` | `IMAP_USERNAME` and an OAuth2 access token in `IMAP_OAUTH_TOKEN`. Microsoft no longer allows passwords. The agent does not fetch tokens itself; see `config/outlook.env`. |
| Any other server | no profile | Set `IMAP_HOST` (and `IMAP_PORT`, `IMAP_AUTH` if needed) |

Try it. The first command only reads and logs; nothing in the mailbox changes:

```bash
python -m email_agent.main --source imap --profile gmail --limit 5                 # read only
python -m email_agent.main --source imap --profile gmail --limit 5 --save-drafts   # also create drafts
```

With Docker, use the same flags: `docker compose run --rm email-agent --source imap --profile gmail --limit 5`.

What it touches in your mailbox:

- It reads only **unread** mail from the last `MAIL_DAYS` days (default 7), and leaves it unread.
- With `--save-drafts`, it adds reply drafts to your Drafts folder.
- With `--apply-labels`, it adds labels such as `Triage/customer_complaint` (Gmail labels, or IMAP keywords
  on other servers; Outlook does not support this).

---

## 6. Sending replies (optional)

By default nothing is sent. A person opens each draft and clicks Send. To send some replies automatically,
set this in `.env`:

```bash
AUTO_SEND=true
AUTO_SEND_CATEGORIES=customer_complaint,kyc_document   # the default list
MAX_SENDS_PER_RUN=5
```

A reply is sent only if **all** of these are true:

- its category is in the list (phishing can never be added);
- no guardrail raised any flag (any doubt keeps it a draft);
- the per-run limit is not reached;
- it was never sent before (`output/sent.jsonl` is the record).

Replies always go to the original `From` address, never to `Reply-To`. In mock mode, "sent" replies are
written to `output/sent/` instead.

---

## 7. Run it all the time (deploy)

```bash
MAIL_PROFILE=gmail docker compose up -d
```

This starts the agent and the dashboard. The agent waits for new mail (IMAP IDLE), so it reacts within
seconds. It labels new emails and saves reply drafts. It sends only if `AUTO_SEND` is on.
You can tune it in `.env` with `MAIL_LIMIT` (emails per run, default 20) and `WATCH_INTERVAL`
(max seconds between checks, default 1500).

The agent container is locked down: non-root user, read-only file system, all Linux capabilities dropped.
`./output` is the only folder it can write to.

---

## 8. Dashboard (Grafana)

Open **http://localhost:3000** and log in with `admin` / `password`.
Grafana is reachable **only from your own machine**. To use your own password, set
`GRAFANA_ADMIN_PASSWORD` in `.env` *before* the first start.

The **Email Agent** dashboard shows:

- counts: emails, replies sent, drafts, LLM calls and errors, guardrail flags;
- charts: emails by status and category, LLM speed and token use;
- an **Activity log** you can search (an email id, a sender, any word);
- a **Recent emails** table; click a trace id to see every step for that email.

In Docker, the full AI prompts and answers are recorded so you can search them (`OTEL_CAPTURE_CONTENT=true`).
They contain customer email text. Set `OTEL_CAPTURE_CONTENT=false` in `.env` to turn this off.

---

## 9. Running the tests

```bash
pytest                       # all offline tests (no key, no network)
pytest tests/test_agent.py   # one file
pytest -m live               # optional: one test against the real LLM (needs a key)
ruff check . && ruff format --check .   # code style
```

The tests fake IMAP, SMTP and the LLM. One end-to-end test checks that all 23 mock emails get the
expected result. The same checks run on GitHub Actions for every push (`.github/workflows/ci.yml`).

---

## 10. Output files

Everything is written to `output/` (in `.gitignore`, because it contains email data):

| File | What is in it |
|---|---|
| `results.jsonl` | One line per email: status, category, guardrail flags |
| `review_queue.jsonl` | Emails a person should look at: `needs_review`, `quarantined`, `escalated` |
| `drafts/*.eml` | Reply drafts (mock mode, with `--save-drafts`) |
| `sent.jsonl` | Record of every automatically sent reply |
| `state-*.json` | Which emails are already done, so nothing is handled twice |

What each status means:

| Status | Meaning |
|---|---|
| `drafted` | A reply draft is ready |
| `escalated` | An incident: reply drafted and added to the review queue |
| `filed` | A bank statement: details extracted, no reply needed |
| `quarantined` | Phishing or an attack: no reply, added to the review queue |
| `needs_review` | The agent was not sure, or a check failed: a person decides |
| `no_reply` | A simple "thank you": recorded, not answered |
| `ignored` | Not a business email (newsletter, auto-reply) |
| `error` | Something failed; it is retried on the next run |

---

## 11. Command-line options

```
python -m email_agent.main [options]

--source mock|imap     where emails come from (default: mock)
--profile NAME         mailbox settings from config/NAME.env: gmail, outlook, yahoo
--limit N              max emails per run (default: 10)
--save-drafts          save replies as drafts
--apply-labels         add Triage/* labels to emails
--watch                keep running and handle new mail as it arrives
--interval SECONDS     max wait between checks with --watch (default: 300)
--fresh                forget which emails were handled and start over
--only ID [ID ...]     reprocess just these emails, e.g. --only m06 m08
```

All settings (limits, model, placeholders) are listed with comments in [`.env.example`](.env.example) and
explained in [ARCHITECTURE.md, section 10](ARCHITECTURE.md#10-configuration).

---

## 12. Project layout

```
email_agent/          the agent (start reading at main.py, then agent.py)
  main.py             command line, settings loading, watch loop
  agent.py            the per-email pipeline and the LLM tool loop
  guardrails.py       all safety checks (input cleaning, injection, output checks)
  tools.py            the 3 tools the AI may call
  llm.py              Gemini and Hugging Face HTTP clients
  mailbox.py          mock inbox and IMAP mailbox
  actions.py          what happens after a decision: logs, drafts, labels, sending
  sender.py           SMTP sending (only with AUTO_SEND)
  state.py            remembers processed emails
  models.py           Pydantic data models
  categories.py       category descriptions and reply instructions (edit to change behaviour)
  config.py           all settings
  telemetry.py        OpenTelemetry setup
config/               provider profiles (gmail, outlook, yahoo) - no secrets
data/mock_inbox.json  23 sample emails, including 6 attack emails
tests/                offline test suite
observability/        Collector, Tempo, Loki, Prometheus and Grafana config
secrets/              your secret files for Docker (ignored by git)
```

---

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `quota exhausted` or HTTP 429 | The free Gemini daily limit is used up. Wait until tomorrow, use a smaller `--limit`, try another `GEMINI_MODEL`, or switch to `LLM_PROVIDER=huggingface` with `HF_TOKEN`. Emails that were not handled are picked up on the next run. |
| `Set GEMINI_API_KEY ...` | The key is missing: add it to `.env` (local) or `secrets/gemini_api_key` (Docker). |
| HTTP 400 `API key not valid` | The key is wrong or has extra characters. Copy it again. |
| Docker: an error that a secret file does not exist | All four files in `secrets/` must exist, even if empty. See [section 3](#3-quick-start-docker). |
| Docker on Linux: `Permission denied` | Run the `chown 10001` commands at the end of [section 3](#3-quick-start-docker). |
| IMAP login failed | Gmail/Yahoo need an **app password**, not your normal password. Check that IMAP is enabled in the mailbox settings. |
| Nothing happens on a second run | Emails already handled are skipped. Use `--fresh` or `--only <id>`. |
| `state file ... is corrupt` | Delete the named file in `output/`. |
