# Email Triage Agent

An AI agent that reads incoming emails, decides what kind of email each one is, and writes a reply **draft**
for a person to check and send.

It sorts emails into seven categories: **incident**, **bank statement**, **financial disbursement**,
**payment request**, **customer complaint**, **KYC document** and **phishing**. Emails that fit none of them
are ignored.

Key points:

- **Careful sending.** Clean replies to complaints and KYC emails are sent automatically. Anything with
  the slightest doubt is saved as a draft for a person. One setting (`AUTO_SEND=false`) turns sending off.
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
6. [Sending replies](#6-sending-replies)
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
> The agent makes about **1-2 requests per email**, and many emails (attacks, auto-replies) need none.
> For a first try, use `--limit 5` (the default is 10). If you hit the limit, see [Troubleshooting](#13-troubleshooting).

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

**Step 4 - Run**

```bash
python -m email_agent.main --limit 5
```

With no mailbox in `.env` (`IMAP_USERNAME` empty), this uses the built-in **mock inbox**.
To use your real mailbox, see [section 5](#5-connect-a-real-mailbox) - the command stays the same.

At the end you will see a table like this:

```
ID       STATUS        CATEGORY                  CONF  FLAGS
m01      escalated     incident                  0.95
m03      filed         bank_statement            0.98
m05      drafted       financial_disbursement    0.90
...
Accuracy vs expected status: 5/5
```

In mock mode, replies are written to `output/sent/*.eml` (sent) and `output/drafts/*.eml` (drafts).
Open them with any mail app or text editor.
The full results are in `output/results.jsonl`.

To run the same emails again, add `--fresh`. Without it, emails that were already handled are skipped.

---

## 3. Quick start: Docker

Docker runs the agent **and** the dashboard (Grafana) with one command.

**Step 1 - Put your Gemini key in `.env`**

```bash
cp .env.example .env             # Windows PowerShell: Copy-Item .env.example .env
```

Open `.env` and set `GEMINI_API_KEY=your-key` (and the mailbox settings, if you want a real mailbox).

**Step 2 - Choose a keystore password**

Run `./start.sh` (Windows PowerShell: `.\start.ps1`) once. It creates the file `keystore_password` with the
placeholder `CHANGE_ME` and stops. Open the file, replace `CHANGE_ME` with your own password (at least 8
characters) and save it. The file is in `.gitignore`, so it is never committed.

**Step 3 - Start**

```bash
./start.sh                       # Windows PowerShell: .\start.ps1
```

It builds the image and starts the agent and the dashboard. While building, it puts the secrets from your
`.env` into an encrypted keystore **inside the image**; the image's copy of `.env` has only references
(`GEMINI_API_KEY=keystore:GEMINI_API_KEY`). Your own `.env` is not changed. See [section 4](#4-where-secrets-go).

With no mailbox configured, the agent works on the mock inbox. It keeps running and checks again every
25 minutes (`WATCH_INTERVAL`). Watch it with `docker compose logs -f email-agent`.

Open the dashboard at **http://localhost:3000** (login `admin` / `password`). See [section 8](#8-dashboard-grafana).

Other useful commands:

```bash
docker compose run --rm email-agent --limit 5   # one run, then stop
docker compose run --rm tests                   # offline tests
docker compose down                             # stop everything
```

Changed `.env`? Run the start script again: it rebuilds the image with the new values and restarts the agent.

**Linux only:** the container runs as user ID 10001. Let it write the output folder: `sudo chown 10001 output`.

---

## 4. Where secrets go

**Your `.env` keeps every setting with the real values.** Local Python runs read it as it is.

**The Docker image gets an encrypted keystore instead.** While the image is built, the four secrets
(`GEMINI_API_KEY`, `HF_TOKEN`, `IMAP_PASSWORD`, `IMAP_OAUTH_TOKEN`) are moved into `/app/keystore.enc`
(AES via Fernet, key from the password via scrypt). The image's copy of `.env` has only references, like
`HF_TOKEN=keystore:HF_TOKEN`, and the agent looks them up when it starts.

| What | Where |
|---|---|
| Real values | Your `.env` (in `.gitignore`, never committed, never copied into the image) |
| Encrypted keystore + `.env` with references | Inside the Docker image (`/app/keystore.enc`, `/app/.env`) |
| Keystore password | `keystore_password` in the project folder, set by you (in `.gitignore`, never committed or copied into the image) |

How it stays safe in Docker:

- `.env` and the password reach the build as **build secrets**, which are never stored in an image layer.
  The image holds no plain secret, and `docker history` shows none.
- At runtime the password is a **Docker secret**, mounted at `/run/secrets/keystore_password`. It is not
  in the image, and `docker inspect` shows only the references.
- `./start.sh` rebuilds only when `.env` or the password changed.
- **Changing the password is safe.** The running agent unlocked its keystore at startup and keeps working.
  Run `./start.sh` after the change: it builds a new image with the new password, then replaces the agent.

To see what is in the image's keystore (names only):
`docker compose run --rm --entrypoint python email-agent -m email_agent.keystore list`.

Other safety rules in the code:

- Secrets are stored as Pydantic `SecretStr`, so they print as `**********` in logs and errors.
- They are never written to traces, logs or output files.
- `config/*.env` (the provider profiles) hold **no** secrets. They are built into the Docker image.

More detail: [SECURITY.md](SECURITY.md).

---

## 5. Connect a real mailbox

The agent works with any IMAP mailbox. You set three things: the **provider** (`MAIL_PROFILE`), your
**address** (`IMAP_USERNAME`) and an **app password** (`IMAP_PASSWORD`). The server names and ports are
already in `config/<provider>.env`.

### Gmail / Google Workspace

1. **Turn on 2-Step Verification** for the account: [myaccount.google.com/security](https://myaccount.google.com/security).
   Google only offers app passwords when it is on.
2. **Create an App Password:** [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   Give it any name (for example "email agent") and copy the 16-letter password. You see it only once.
3. **Check that IMAP is on:** Gmail > Settings > See all settings > *Forwarding and POP/IMAP* > *Enable IMAP*.
   (It is on by default for most accounts. Google Workspace admins can turn it off for the whole company.)
4. **Add the login to `.env`:**

   ```bash
   MAIL_PROFILE=gmail
   IMAP_USERNAME=you@gmail.com
   # the App Password, without spaces
   IMAP_PASSWORD=abcdefghijklmnop
   ```

5. **Run:** `./start.sh` (Docker) or `python -m email_agent.main --limit 5`.

### Yahoo Mail

1. Yahoo > Account info > **Account security** > **Generate app password**. Copy it.
2. In `.env`: `MAIL_PROFILE=yahoo`, `IMAP_USERNAME=you@yahoo.com`, `IMAP_PASSWORD=<app password>`.
3. Run as above.

### Outlook.com / Microsoft 365

Microsoft no longer accepts passwords for IMAP. You need an **OAuth2 access token** instead.

1. Register an app in [Microsoft Entra](https://entra.microsoft.com) > App registrations, and give it the
   delegated permissions `IMAP.AccessAsUser.All` and `SMTP.Send`.
2. Get an access token for your account with these scopes (for example with the MSAL device-code flow).
   The agent does not fetch tokens itself.
3. In `.env`: `MAIL_PROFILE=outlook`, `IMAP_USERNAME=you@outlook.com`, `IMAP_OAUTH_TOKEN=<token>`.
4. Tokens last about an hour. The agent reads the token again before every check, so a script that refreshes
   it can simply store the new one in `.env` (Docker: then run `./start.sh` again).

Outlook does not support custom labels, so the outlook profile turns labels off.

### Any other IMAP server

Skip `MAIL_PROFILE` and set the servers yourself in `.env`: `IMAP_HOST`, `SMTP_HOST`, and `IMAP_PORT` /
`SMTP_PORT` if they are not 993 / 465. Without `SMTP_HOST`, nothing is sent; replies are saved as drafts.

### What it does in your mailbox

- It reads only **unread** mail from the last `MAIL_DAYS` days (default 7), and leaves it unread.
- It adds labels such as `Triage/customer_complaint` (Gmail labels, or IMAP keywords on other servers).
- It **sends** clean replies to complaints and KYC emails (see [section 6](#6-sending-replies)). You find
  them in your Sent folder.
- Every other reply goes to your **Drafts** folder for a person to check and send.

To turn things off:

| To stop... | Use |
|---|---|
| sending | `AUTO_SEND=false` in `.env` (replies become drafts) |
| drafts | `--no-save-drafts` |
| labels | `--no-apply-labels` (or `APPLY_LABELS=false` in `.env`) |

For a first look without changing anything in the mailbox:
`AUTO_SEND=false` in `.env`, then `python -m email_agent.main --limit 5 --no-save-drafts --no-apply-labels`.

---

## 6. Sending replies

Sending is **on by default**, but only for safe cases. A reply is sent only if **all** of these are true:

- its category is in `AUTO_SEND_CATEGORIES` (default `customer_complaint,kyc_document`; phishing can never be added);
- no guardrail raised any flag (any doubt keeps it a draft);
- the per-run limit `MAX_SENDS_PER_RUN` (default 5) is not reached;
- it was never sent before (`output/sent.jsonl` is the record).

Everything else is saved as a draft. Replies always go to the original `From` address, never to `Reply-To`.
In mock mode, "sent" replies are written to `output/sent/` instead.

To never send, set `AUTO_SEND=false` in `.env`.

---

## 7. Run it all the time (deploy)

```bash
docker compose up -d
```

This starts the agent and the dashboard, using the mailbox from `.env` ([section 5](#5-connect-a-real-mailbox)).
The agent waits for new mail (IMAP IDLE), so it reacts within seconds. It labels new emails, sends the
clean replies and saves the rest as drafts.
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
| `drafts/*.eml` | Reply drafts (mock mode) |
| `sent/*.eml` | "Sent" replies (mock mode) |
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

--source mock|imap     where emails come from (default: imap if IMAP_USERNAME is set, otherwise mock)
--profile NAME         mailbox settings from config/NAME.env: gmail, outlook, yahoo (default: MAIL_PROFILE)
--limit N              max emails per run (default: 10)
--no-save-drafts       do not save replies as drafts (default: saved)
--no-apply-labels      do not add Triage/* labels (default: added, except Outlook)
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
  tools.py            the 2 tools the AI may call (submit a triage, submit a reply)
  llm.py              Gemini and Hugging Face HTTP clients
  mailbox.py          mock inbox and IMAP mailbox
  actions.py          what happens after a decision: logs, drafts, labels, sending
  sender.py           SMTP sending (only with AUTO_SEND)
  state.py            remembers processed emails
  models.py           Pydantic data models
  categories.py       category descriptions and reply instructions (edit to change behaviour)
  config.py           all settings
  keystore.py         encrypted keystore for the secrets (built into the Docker image)
  telemetry.py        OpenTelemetry setup
config/               provider profiles (gmail, outlook, yahoo) - no secrets
data/mock_inbox.json  23 sample emails, including 6 attack emails
tests/                offline test suite
observability/        Collector, Tempo, Loki, Prometheus and Grafana config
```

---

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `quota exhausted` or HTTP 429 | The free Gemini daily limit is used up. Wait until tomorrow, use a smaller `--limit`, try another `GEMINI_MODEL`, or switch to `LLM_PROVIDER=huggingface` with `HF_TOKEN`. Emails that were not handled are picked up on the next run. |
| `Set GEMINI_API_KEY ...` | The key is missing: add it to `.env` and run `./start.sh` again (or just run locally). |
| `keystore password file ... is missing` | Start with `./start.sh` (it passes `keystore_password` to Docker). |
| `HF_TOKEN points to keystore:HF_TOKEN, which is not in the keystore` | Your `.env` should have the real value, not the reference. Fix it and run `./start.sh` again. |
| `Wrong keystore password or damaged keystore` | The password does not match this keystore. To start over, see [section 4](#4-where-secrets-go). |
| HTTP 400 `API key not valid` | The key is wrong or has extra characters. Copy it again. |
| `Set IMAP_HOST ...` | Add `MAIL_PROFILE=gmail` (or `outlook`, `yahoo`) to `.env`. |
| Docker on Linux: `Permission denied` | Run the `chown 10001` commands at the end of [section 3](#3-quick-start-docker). |
| IMAP login failed | Gmail/Yahoo need an **app password**, not your normal password. Check that IMAP is enabled in the mailbox settings ([section 5](#5-connect-a-real-mailbox)). |
| Replies are drafts, never sent | Check that `AUTO_SEND` is not `false` and the category is in `AUTO_SEND_CATEGORIES`. Any guardrail flag in the results table also keeps a reply as a draft. |
| Nothing happens on a second run | Emails already handled are skipped. Use `--fresh` or `--only <id>`. |
| `state file ... is corrupt` | Delete the named file in `output/`. |
