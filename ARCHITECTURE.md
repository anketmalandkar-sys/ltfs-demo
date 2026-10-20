# Email Triage Agent - Architecture

This document explains how the agent works inside. For setup, see [README.md](README.md).

In one paragraph: the agent reads emails (from any IMAP mailbox or a mock inbox), puts each one into a
fixed business category, and prepares a matching reply. An LLM (Gemini or a Hugging Face model)
does the reading and writing, but only through three narrow **tools**. Plain-code **guardrails** check
everything before the LLM sees an email and again after it answers. Only clean replies in allowed
categories are sent (`AUTO_SEND`, on by default); everything else stays a draft (section 5).

---

## 1. Overview

| | |
|---|---|
| **Input** | Any IMAP inbox over verified TLS (password, app password or OAuth2 token), or `data/mock_inbox.json` |
| **Output** | `output/results.jsonl`, `output/review_queue.jsonl`, plus sent replies, drafts and labels in the mailbox |
| **LLM** | Gemini (`generateContent` with function calling) or the Hugging Face router (OpenAI-style tools), over plain HTTP |
| **Stack** | Python 3.12, Pydantic v2, `requests`, `imaplib`, OpenTelemetry, Docker Compose, Collector + Tempo + Loki + Prometheus + Grafana |
| **Not in scope** | Sending anything the guardrails doubted, reading attachments (such as PDF statements), back-and-forth conversations with customers |

### Categories and outcomes

| Category | Outcome (`Status`) | Reply |
|---|---|---|
| `incident` | `escalated` | Acknowledgement with priority; goes to the review queue |
| `bank_statement` | `filed` | None; period, masked account and balance are extracted |
| `financial_disbursement` | `drafted` | Confirms amount, reference and masked account exactly as written |
| `payment_request` | `drafted` | Acknowledges invoice, amount and due date |
| `customer_complaint` | `drafted` | Apology, `{ticket_id}`, `{sla}` |
| `kyc_document` | `drafted` | Acknowledges documents, lists missing ones |
| `phishing_suspicious` | `quarantined` | None |
| anything else | `ignored` | None; only the id and status are kept |

Two more outcomes can happen to any email:

- `needs_review`: a person must look at it.
- `error`: something failed; it is retried on the next run.

A short "thank you" or "got it" with nothing else to act on becomes `no_reply`. It is recorded (results,
logs, dashboard, label `Triage/NoReply`) but never answered, and it costs no LLM call. This check is strict:
any question, number, attachment or problem word (*but, still, not, please, issue, ...*) means normal
handling. It also runs only **after** the injection scan, so a "thanks" that quotes a poisoned thread is
still quarantined.

---

## 2. Components

```mermaid
flowchart LR
    CLI["main.py<br/>CLI and watch loop"] --> AG["agent.py<br/>per-email pipeline"]
    CFG["config.py<br/>Settings: env, profile,<br/>keystore"] --> CLI
    AG --> MB["mailbox.py<br/>MockMailbox / ImapMailbox<br/>ImapWatcher (IDLE)"]
    AG --> GR["guardrails.py<br/>sanitize, inspect, check_result"]
    AG --> TL["tools.py<br/>ToolSession: 3 tools"]
    AG --> LLM["llm.py<br/>GeminiClient / HuggingFaceClient"]
    AG --> ST["state.py<br/>processed ids, threads, attempts"]
    AG --> AC["actions.py<br/>logs, review queue, drafts, labels"]
    AC --> MB
    CAT["categories.py<br/>descriptions + reply guidance"] --> AG
    MB <--> GMAIL[("IMAP server<br/>Gmail, Outlook, ...")]
    LLM <--> API[("Gemini API /<br/>HF router")]
    AG -. spans .-> OT["telemetry.py<br/>OpenTelemetry"]
    OT -. OTLP/HTTP .-> JG[("Collector -> Tempo,<br/>Loki, Prometheus -> Grafana")]
```

| Module | What it does |
|---|---|
| `models.py` | Pydantic models and enums: `Email`, `Triage`, `ReplyDraft`, `EmailResult`, `RunReport` |
| `config.py` | `Settings` (pydantic-settings). Secrets are `SecretStr` |
| `keystore.py` | Encrypted keystore (Fernet + scrypt): `build` makes it while the Docker image is built; `list` shows the names |
| `categories.py` | One dict: category -> (description, reply guidance) |
| `mailbox.py` | `connect / fetch_new / save_draft / apply_label / close`. There is no send method. `ImapWatcher` waits for new mail |
| `guardrails.py` | All plain-code safety checks |
| `tools.py` | Tool argument models, the per-email `ToolSession`, and the JSON schemas for each provider |
| `llm.py` | Two HTTP clients with the same `chat()` method; retries on 429 and 5xx |
| `agent.py` | The pipeline, the tool loop, the LLM budget and tracing |
| `state.py` | Remembers processed emails and retry counts (atomic JSON file) |
| `actions.py` | What happens after the decision: logs, review queue, drafts, labels, sending |
| `sender.py` | `SmtpSender` / `MockSender`. Used only by `actions.py`, only when `AUTO_SEND` is on (default) |
| `telemetry.py` | OpenTelemetry setup: `none`, `console` or `otlp` |

### Mailbox: one IMAP client for every provider

`ImapMailbox` speaks plain IMAP. It uses Gmail's extensions only when the server says it supports them
(`X-GM-EXT-1`). Provider differences live in `config/<profile>.env` (host, port, login method), not in code.

| Need | Gmail | Any other IMAP server |
|---|---|---|
| Login | `LOGIN` with an App Password, or XOAUTH2 | `LOGIN` with a password or app password, or XOAUTH2 (Outlook / Microsoft 365) |
| Thread id (to spot follow-ups) | `X-GM-THRID` | First id in `References`, else `In-Reply-To`, else the email's own `Message-ID` |
| Labels (on by default; `--no-apply-labels`) | `X-GM-LABELS` | IMAP keyword (`+FLAGS`). Servers without custom keywords give a clear error |
| Drafts folder | Found by its `\Drafts` flag (any language) | `\Drafts` flag (RFC 6154), or `IMAP_DRAFTS_FOLDER` |
| New-mail push | IDLE | IDLE if supported, otherwise polling every `--interval` |

The connection always uses TLS on port 993 with `ssl.create_default_context()`, so the certificate and host
name are checked. (`imaplib` does **not** check them by default.)

### Watch loop: push instead of polling

With `--watch`, a separate "watcher" connection waits in IMAP IDLE (RFC 2177). The server tells it when new
mail arrives. The agent's own connection opens only when there is work. `--interval` is the longest the
watcher waits, as a safety net in case a push is missed.

```mermaid
sequenceDiagram
    participant M as main.py
    participant W as ImapWatcher
    participant S as IMAP server
    participant A as Agent.run
    loop every cycle
        M->>A: run_once (connect, fetch unread, process, close)
        M->>M: reload settings (rotated secrets)
        M->>W: wait(interval)
        W->>S: STATUS INBOX (UIDNEXT)
        alt UIDNEXT changed since last wake-up
            W-->>M: True (mail came in during the run)
        else
            W->>S: IDLE
            alt "* n EXISTS" before the deadline
                S-->>W: new mail
                W->>S: DONE, STATUS (remember UIDNEXT)
                W-->>M: True
            else deadline (at most 25 min) or dropped connection
                W-->>M: False, check anyway
            end
        end
    end
```

Python 3.12's `imaplib` has no IDLE, so `ImapWatcher._idle` adds it in about 15 lines. It uses a real
deadline, not only a socket timeout, because some servers send keep-alive lines. Searching for unread mail
again is cheap, and the state file makes sure no email is handled twice.

---

## 3. Per-email pipeline

```mermaid
flowchart TD
    A[fetch_new] --> B{already done?}
    B -- yes --> SKIP[skip]
    B -- no --> C{LLM budget left?}
    C -- no --> STOP[stop run]
    C -- yes --> D["sanitize<br/>HTML to visible text, NFKC, strip invisible chars,<br/>strip quoted history, redact PII, truncate"]
    D --> E{auto-reply, bounce<br/>or own mail?}
    E -- yes --> IGN[ignored]
    E -- no --> F{empty?}
    F -- yes --> NR1[needs_review]
    F -- no --> G{thread already handled?}
    G -- yes --> NR2["needs_review (follow_up)"]
    G -- no --> H["inspect raw email<br/>injection patterns + sender authenticity"]
    H --> I{strong signal or<br/>2+ sender signals?}
    I -- yes --> Q["quarantined<br/>(LLM never sees it)"]
    I -- no --> J{bulk and nothing<br/>business-related?}
    J -- yes --> IGN2[ignored]
    J -- no --> K["LLM tool loop<br/>read_email, submit_triage, submit_reply"]
    K --> L["check_result<br/>confidence, weak signals, sender, grounding, canary"]
    Q --> L
    IGN --> L
    IGN2 --> L
    NR1 --> L
    NR2 --> L
    L --> M[fill placeholders in draft]
    M --> N["actions.apply<br/>log, review queue, optional draft and label"]
    N --> O["state.record<br/>done, or attempts+1 on error"]
```

Each email gets its **own** LLM conversation. This keeps the context small, keeps a failure local, and
means one poisoned email cannot affect the next one.

When something goes wrong:

- Any exception gives `status=error`. The email is retried on the next run, and after `MAX_ATTEMPTS`
  failures it goes to `needs_review`.
- A **fatal LLM error** stops the whole run at once, with the reason printed: HTTP 401, 403 or 404,
  a 400 "API key not valid", or a 429 asking for a wait longer than 90 seconds (quota used up). Every
  request would fail the same way, so there is no point going on. The email does not lose a retry attempt.

---

## 4. Tool-calling sequence

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent
    participant G as Guardrails
    participant L as LLM (Gemini / HF)
    participant T as ToolSession

    A->>G: sanitize + inspect(email)
    G-->>A: clean email, Inspection (not blocked)
    A->>L: system prompt (rules, categories, canary) + "Triage email id 'm08'"
    L-->>A: tool_call read_email(email_id)
    A->>T: call("read_email")
    Note over T: email_id pinned to m08<br/>args validated by Pydantic
    T-->>A: sanitized email wrapped in random-nonce email_content tags
    A->>L: tool result
    L-->>A: tool_call submit_triage(category, confidence, …)
    A->>T: call("submit_triage")
    Note over T: must follow read_email<br/>only once, enum-checked
    T-->>A: "ok: now call submit_reply"
    A->>L: tool result
    L-->>A: tool_call submit_reply(subject, body)
    A->>T: call("submit_reply")
    Note over T: refused for no-reply categories
    T-->>A: "ok: reply recorded"
    A->>G: check_result(triage, draft)
    G-->>A: drafted / needs_review / quarantined
    A->>A: fill {ticket_id} {sla} {signature}
```

If the model answers with text instead of a tool call, it gets one reminder. If it does it again, the email
goes to `needs_review`. `MAX_STEPS` limits each conversation, and `MAX_LLM_CALLS_PER_RUN` limits each run.

---

## 5. Guardrail layers

```mermaid
flowchart LR
    subgraph IN["1. Input"]
        I1[HTML to visible text]
        I2[NFKC + strip zero-width / bidi / tag chars]
        I3[strip quoted and forwarded history]
        I4[redact card, Aadhaar, PAN, OTP, long numbers]
        I5[truncate 8k chars]
    end
    subgraph INJ["2. Injection and poisoning"]
        J1[strong patterns: quarantine]
        J2[weak patterns: no auto reply]
        J3[SPF / DKIM / DMARC, brand on free mail, Reply-To mismatch]
        J4[nonce-wrapped email content]
    end
    subgraph TOOL["3. Tool calls"]
        T1[3 tools only, no send / forward / fetch]
        T2[Pydantic-validated args]
        T3[email_id pinned, call order enforced]
        T4[max steps, LLM budget]
    end
    subgraph OUT["4. Output"]
        O1[confidence threshold]
        O2[category agrees with signals]
        O3[no URLs, emails, phones in reply]
        O4[every number grounded in source]
        O5[no commitments or unverified payment status, canary check]
    end
    subgraph OPS["5. Operations"]
        P1[sending only per allowed category, clean replies only, capped; rest are drafts]
        P2[IMAP read-only + BODY.PEEK, verified TLS]
        P3[SecretStr, secrets as files; no bodies or secrets on spans]
        P4[dedupe, retries, review queue]
    end
    IN --> INJ --> TOOL --> OUT --> OPS
```

### Automatic sending (`AUTO_SEND`)

On by default (`AUTO_SEND=false` turns it off). `Actions` sends a reply over SMTP (instead of only saving a draft) only if
every rule below holds:

| Rule | Why |
|---|---|
| Category is in `AUTO_SEND_CATEGORIES` (default `customer_complaint,kyc_document`; `phishing_suspicious` is rejected) | Risky categories stay with a person |
| `guardrail_flags` is empty | Any injection, sender or output doubt keeps it a draft |
| Fewer than `MAX_SENDS_PER_RUN` sent in this run | A flood of email or a bad prompt can't send hundreds of replies |
| Message id is not in `output/sent.jsonl` | Never sent twice, even after a crash or `--only` / `--fresh` |
| Recipient is the original `From` | Never `Reply-To` (`build_reply`) |

Sent replies are flagged `sent` in `results.jsonl` and recorded in `sent.jsonl` (the audit log). Escalated
incidents still go to the review queue even when the acknowledgement was sent. Sending makes no extra LLM
call: the text is the same reply that already passed every output check.

### Prompt injection and message poisoning

The agent assumes every email is hostile. The goal: a successful injection can at most push an email into
`needs_review`. It must never be able to send mail, leak data, or change how other emails are handled.

| # | Defence | Where |
|---|---|---|
| 1 | Normalize Unicode and remove zero-width, bidi-override and tag characters, so `i​g​n​o​r​e` becomes `ignore` | `guardrails.normalize` |
| 2 | Turn HTML into visible text only. `display:none`, `font-size:0`, white text, comments and scripts are dropped and flagged | `guardrails.html_to_text` |
| 3 | Drop quoted history (`>` lines, "On ... wrote:", original/forwarded blocks), so an old thread can't carry instructions | `guardrails.strip_quoted` |
| 4 | Scan the **raw** email (hidden text and quotes included). **Strong** hits are quarantined before any LLM call: "ignore previous instructions", role markers (`system:`, `<\|im_start\|>`, `[INST]`), our tool names, mass-forward requests, requests to change a beneficiary or bank details. **Weak** hits ("act as", "system prompt", long encoded blobs, hidden text) mean no automatic reply | `guardrails.detect_injection`, `inspect` |
| 5 | Sender checks: SPF/DKIM/DMARC failure, a brand name on a free-mail address, a different Reply-To. Two or more signals quarantine the email; one signal sends financial categories to review | `guardrails.sender_signals` |
| 6 | Structural isolation. Instructions exist only in the system prompt. The email reaches the model only as a tool result inside `<email_content nonce="random">`, and look-alike tags in the body are escaped | `tools.render_email` |
| 7 | Least privilege. The model can read one email (its id is pinned) and submit a triage and a reply. It has no tool to send, forward, list or fetch, and every email gets a fresh context | `tools.ToolSession` |
| 8 | Output checks. A canary token catches system-prompt leaks. Every number in a reply must appear in the email. URLs, email addresses, phone numbers and promises ("we will refund") are blocked. So are payment statuses the email never states ("is being processed", "will be credited") and claims of actions nobody took ("has been sent to accounts payable", "we have notified the team"; "escalated" is allowed only for escalated incidents). Reply-To is never used as the recipient | `guardrails.check_result`, `mailbox.build_reply` |
| 9 | IDs, contacts and links come only from code. `{ticket_id}`, `{sla}` and `{signature}` are filled in after the checks. The ticket id is a hash of the email (`LTFS-1A2B3C4D`), so reprocessing gives the same one. An LLM-written closing right above `{signature}` is dropped to avoid signing off twice | `actions.fill_placeholders` |

---

## 6. Status lifecycle

```mermaid
stateDiagram-v2
    [*] --> New
    New --> Ignored: auto-reply / bulk / out_of_scope
    New --> NoReply: thank-you / got it (recorded, never answered)
    New --> Quarantined: strong injection / spoofed sender / phishing
    New --> NeedsReview: follow-up / empty / weak signal / low confidence / failed check
    New --> Filed: bank_statement
    New --> Drafted: reply passed all checks
    New --> Escalated: incident
    New --> Error: exception (LLM down, IMAP hiccup)
    Error --> New: next run, attempts below MAX_ATTEMPTS
    Error --> NeedsReview: attempts reach MAX_ATTEMPTS
    Ignored --> [*]
    NoReply --> [*]
    Quarantined --> [*]
    NeedsReview --> [*]
    Filed --> [*]
    Drafted --> [*]
    Escalated --> [*]
```

`NeedsReview`, `Quarantined` and `Escalated` are also written to `output/review_queue.jsonl`.

**Running an email again is safe.** Reprocessing (with `--only`, `--fresh`, or a retry after a crash)
gives the same ticket id and never creates a second draft. If a draft replying to that email already exists
(matched on `In-Reply-To` in the Drafts folder, or in `output/drafts`), it is left alone, because a person
may have edited it. The result is flagged `draft_exists`. To get a new draft, delete the old one first.

With labels on (the default), emails get the label `Triage/NeedsReview`, `Triage/Quarantine` or
`Triage/<category>` (Gmail labels, or IMAP keywords on other servers).

---

## 7. Data models

```mermaid
classDiagram
    class Email {
        id: str
        message_id: str
        thread_id: str
        from_name: str
        from_addr: str
        reply_to: str
        subject: str
        body: str
        headers: dict
        attachment_names: list~str~
        sender_domain() str
    }
    class Triage {
        category: Category
        confidence: float 0..1
        priority: Priority?
        summary: str ≤300
        fields: ExtractedFields
    }
    class ExtractedFields {
        amount, currency, reference
        account_masked, period, due_date
        long digit runs auto-masked
    }
    class ReplyDraft {
        subject: str ≤200
        body: str ≤1500 (LLM writes ≤1200)
    }
    class EmailResult {
        email_id: str
        status: Status
        triage: Triage?
        draft: ReplyDraft?
        guardrail_flags: list~str~
        validator: ignored has no triage/draft
        validator: only drafted/escalated have draft
    }
    class RunReport {
        started_at, provider, model
        results: list~EmailResult~
    }
    class Category {
        <<enum>>
        incident
        bank_statement
        financial_disbursement
        payment_request
        customer_complaint
        kyc_document
        phishing_suspicious
    }
    class Status {
        <<enum>>
        drafted filed escalated
        quarantined ignored no_reply
        needs_review error
    }
    Triage --> ExtractedFields
    Triage --> Category
    EmailResult --> Triage
    EmailResult --> ReplyDraft
    EmailResult --> Status
    RunReport --> EmailResult
```

The tool argument models (`ReadEmailArgs`, `SubmitTriageArgs`, `SubmitReplyArgs`) are Pydantic models too.
Their `model_json_schema()` output is flattened into each provider's function declarations.

---

## 8. Deployment

```mermaid
flowchart LR
    subgraph HOST["Docker host"]
        subgraph C1["email-agent container (non-root, read-only, no capabilities)"]
            APP["python -m email_agent.main --watch"]
            PROF["config/&lt;MAIL_PROFILE&gt;.env<br/>(in image, no secrets)"]
        end
        subgraph OBS["observability (internal network)"]
            COL["otel-collector<br/>OTLP :4318"]
            TEMPO[("Tempo<br/>traces")]
            LOKI[("Loki<br/>logs")]
            PROM[("Prometheus<br/>metrics")]
            GRAF["Grafana<br/>127.0.0.1:3000"]
            COL --> TEMPO & LOKI & PROM
            TEMPO & LOKI & PROM --> GRAF
        end
        VOL[("./output volume<br/>results, review queue,<br/>state, mock drafts")]
        ENV[".env (real values)<br/>build secret only"]
        SEC[("keystore_password<br/>Docker secret")]
    end
    ENV -. "build: keystore.enc + .env with references" .-> APP
    PROF -.->|"MAIL_PROFILE"| APP
    SEC -.->|"/run/secrets (read-only)"| APP
    APP --> VOL
    APP -- "IMAP TLS 993 (verified), IDLE" --> GM[("IMAP server")]
    APP -- HTTPS --> LLMAPI[("Gemini / HF router")]
    APP -- "OTLP HTTP: traces, metrics, logs" --> COL
    USER(["You"]) -- "browser localhost:3000" --> GRAF
```

- The `Dockerfile` has three stages: `base`, `test` (runs pytest) and `runtime` (the default; runs as a
  non-root user).
- `.dockerignore` leaves out `.env`, `output/` and `.venv/`, so no secret ends up in an image.
- `restart: unless-stopped` together with `--watch` keeps the agent running. It wakes on new mail and does a
  full check at least every 25 minutes (`--interval 1500`). Wrong IMAP credentials stop the container with a
  clear message instead of retrying forever.

### Security of the deployment

- **Secrets are in an encrypted keystore, not environment variables.** `GEMINI_API_KEY`, `HF_TOKEN`,
  `IMAP_PASSWORD` and `IMAP_OAUTH_TOKEN` are put into an encrypted keystore while the image is built
  (`.env` and the password are build secrets, never stored in a layer). The password is a Docker secret at
  runtime, from `keystore_password` (set by the user, never committed). Environment variables would show up in
  `docker inspect` and `/proc/*/environ`. The image's `.env` holds only references (`HF_TOKEN=keystore:HF_TOKEN`), and
  `main.resolve_references` replaces them with the keystore values at startup. If a secret still arrives as an environment variable, a warning is logged (with the
  variable name, never the value).
- **Change a secret:** edit `.env` and run `./start.sh`. The image is rebuilt (the `CONFIG_HASH` build argument
  changes with `.env`) and the agent restarts with the new keystore.
- **Hardened container:** non-root UID 10001, `read_only: true` with `tmpfs: /tmp`, `cap_drop: [ALL]` and
  `no-new-privileges`. `./output` is the only writable path.
- **Verified TLS** to the IMAP server (see section 2). Only Grafana is published, on `127.0.0.1:3000`. Its
  default login is `admin` / `password` (change it with `GRAFANA_ADMIN_PASSWORD` in `.env`), and anonymous
  access is off.
- `config/*.env` profiles are built into the image and must never contain secrets.

See also [SECURITY.md](SECURITY.md).

---

## 9. Observability

The agent sends three OpenTelemetry signals over OTLP to the **Collector**. The Collector forwards traces
to **Tempo**, logs to **Loki** and metrics to **Prometheus**. **Grafana** (http://localhost:3000) shows all
three, with an *Email Agent* dashboard on the home page. Logs and traces link to each other via `trace_id`.

| Signal | What you get | Where to look |
|---|---|---|
| Traces | One trace per run: `agent.run` -> `email.process` -> `llm.chat` / `tool.*` / `smtp.send` / `mailbox.save_draft` | Dashboard "Recent emails", or Explore -> Tempo (`{name="email.process"}`) |
| Logs | One line per step, with fields such as `event`, `email_id`, `email_status`, `guardrail_flags`, `trace_id` | Dashboard "Activity log" with its search box, or Explore -> Loki |
| Metrics | Counters and histograms (listed below) | Dashboard panels, or Explore -> Prometheus |

**Log events** (filter with `| event="..."`): `email.received`, `tool.call`, `llm.request`, `llm.response`
(always logged, with `llm_input_tokens`, `llm_output_tokens`, `llm_cost_usd`, `llm_duration_s`), `llm.retry`
(a failed HTTP attempt that is tried again: timeout, rate limit, 5xx), `llm.error`, `guardrail.flag` (one per
flag), `email.decided`, `label.applied`, `draft.saved`, `draft.exists`, `reply.sent`, `mail.new`,
`mail.timeout`, `run.finished`. Note: `sent`, `already_sent` and `draft_exists` are actions, not guardrail
flags.

**Dashboard counts come from these log events** (`count_over_time` in Loki), so they are exact and survive
restarts. For your own Prometheus queries: `increase()` misses the first step of a counter that starts at 1,
so the agent starts every known counter at 0 when it boots (`telemetry.prime_metrics`).

**Metrics** (Prometheus names): `emails_processed_total{status,category}`, `guardrail_flags_total{flag}`,
`llm_requests_total{provider,model,outcome}`, `llm_request_duration_seconds` (histogram),
`llm_tokens_total{direction}`, `llm_retries_total{reason}`, `replies_sent_total{category}`,
`drafts_saved_total`, `mail_wakeups_total{reason}`, `run_duration_seconds` (histogram).

**Content capture.** With `OTEL_CAPTURE_CONTENT=true` (on in `docker-compose.yml`), every LLM call also
records the **raw** request (the system prompt and every message, including the email the model read) and
the response (text, tool calls, token use). They appear as span events `gen_ai.request` / `gen_ai.response`
in Tempo, and as `llm.request` / `llm.response` log lines in Loki, where you can search the text. Each is
capped at 32 KB. This is customer data, so turn it off (`OTEL_CAPTURE_CONTENT=false` in `.env`) when you
don't need it. Credentials are never captured: they are not part of any message, and the Gemini key is sent
in a header.

Search examples:

```
{service_name="email-agent"} |= "EMI"                       # any step mentioning EMI (Loki)
{service_name="email-agent"} | event="llm.response"         # every model reply
{service_name="email-agent"} | email_id="7"                  # everything about one email
{service_name="email-agent"} | event="email.decided" | email_status="quarantined"
{name="llm.chat" && duration > 5s}                           # slow LLM calls (Tempo, TraceQL)
sum by (category) (increase(emails_processed_total[24h]))    # Prometheus
```

Example span tree for one complaint:

```
agent.run                                   emails.processed=1
├── mailbox.fetch_new
└── email.process                           email.id=m08  email.sender_domain=gmail.com
    │                                       triage.status=drafted  triage.category=customer_complaint
    │                                       triage.confidence=0.9  guardrail.flag_count=0
    ├── llm.chat                            gen_ai.system=gemini  gen_ai.request.model=gemini-2.5-flash
    │   └── POST (requests instrumentation)  gen_ai.usage.input_tokens / output_tokens  agent.step=0
    ├── tool.read_email                     tool.ok=true
    ├── llm.chat ── tool.submit_triage
    ├── llm.chat ── tool.submit_reply
    └── mailbox.save_draft                  (unless sent)         draft.created=true|false
```

With `--watch` on IMAP, each cycle also has a `mailbox.wait` span (`mail.new=true|false`) before the next
`agent.run`.

Guardrail hits are recorded as span events named `guardrail.<flag>`, for example
`guardrail.injection:ignore_instructions`. Without content capture, spans and logs carry **metadata only**:
no email bodies or subjects. Error statuses carry only the exception type.

---

## 10. Configuration

Settings are read in this order, highest priority first:

1. environment variables
2. `.env`
3. the provider profile `config/<name>.env` (`--profile`, else `MAIL_PROFILE` from the environment or `.env`)

A secret written as `keystore:NAME` is then replaced by the value `NAME` from the keystore.

All of them are listed with comments in [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `huggingface` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | - / `gemini-2.5-flash` | Gemini key and model |
| `HF_TOKEN` / `HF_MODEL` | - / `Qwen/Qwen2.5-72B-Instruct` | Hugging Face token and a model that supports tool calling |
| `IMAP_HOST` / `IMAP_PORT` | from profile / `993` | IMAP server (TLS only) |
| `MAIL_PROFILE` | - | Provider profile: `gmail`, `outlook` or `yahoo` |
| `IMAP_USERNAME` | - | Login name, usually your email address. If set, the mailbox is used instead of the mock inbox |
| `IMAP_AUTH` | `password` | `password` (password or app password) or `xoauth2` (OAuth2 access token) |
| `IMAP_PASSWORD` / `IMAP_OAUTH_TOKEN` | - | The secret for the chosen `IMAP_AUTH`; usually a `keystore:` reference |
| `IMAP_INBOX` / `IMAP_DRAFTS_FOLDER` | `INBOX` / found by `\Drafts` flag | Folder names, if the server needs them |
| `MAIL_ADDRESS` | `IMAP_USERNAME` | Our own address: draft `From`, and to skip our own mail |
| `MAIL_DAYS` | `7` | Only unread mail from the last N days |
| `APPLY_LABELS` | `true` | Add `Triage/*` labels (the outlook profile sets `false`); `--[no-]apply-labels` overrides |
| `KEYSTORE_PATH` / `KEYSTORE_PASSWORD_FILE` | `keystore.enc` / `keystore_password` | Encrypted keystore and its password file. The Docker image sets them (`/app/keystore.enc`, `/run/secrets/keystore_password`); normally you never set them. Opened only if a setting is a `keystore:` reference. Without a password file, a run at a terminal asks for it. Read from the environment only |
| `AUTO_SEND` | `true` | Send clean replies in `AUTO_SEND_CATEGORIES`; `false` = only drafts (see section 5) |
| `AUTO_SEND_CATEGORIES` | `customer_complaint,kyc_document` | Comma-separated categories that may be sent |
| `MAX_SENDS_PER_RUN` | `5` | Most replies sent in one run |
| `SMTP_HOST` / `SMTP_PORT` | from profile / `465` | SMTP server; 465 = TLS, other ports use STARTTLS. Same login as IMAP |
| `LLM_REQUESTS_PER_MINUTE` | `0` (off) | Client-side speed limit (free Gemini keys: about 2-5). Short 429s are retried up to 6 times using the API's `retryDelay`; a requested wait over 90 s stops the run |
| `LLM_TIMEOUT_SECONDS` | `90` | Per HTTP attempt; slower answers are retried, and each retry is logged (`llm.retry`) |
| `LLM_INPUT_PRICE_PER_MTOK` / `LLM_OUTPUT_PRICE_PER_MTOK` | `0` / `0` | USD per million tokens, for the cost estimate (`llm_cost_usd`); 0 = not tracked |
| `MAX_EMAILS` | `50` | Upper limit for `--limit` |
| `MAX_STEPS` | `6` | LLM turns per email |
| `MAX_LLM_CALLS_PER_RUN` | `200` | Protects your quota if a flood of email arrives |
| `MAX_ATTEMPTS` | `3` | Failed tries before `needs_review` |
| `MAX_BODY_CHARS` | `8000` | Email body is cut to this length |
| `MIN_CONFIDENCE` | `0.6` | Below this, the email goes to `needs_review` |
| `TICKET_PREFIX`, `SLA_TEXT`, `SIGNATURE` | `LTFS`, ... | Placeholder values filled in by code |
| `OUTPUT_DIR`, `MOCK_INBOX` | `output`, `data/mock_inbox.json` | Paths |
| `OTEL_TRACES_EXPORTER` | `none` | `none`, `console` (traces to stdout) or `otlp` (traces, metrics and logs) |
| `OTEL_CAPTURE_CONTENT` | `false` | Also record raw LLM requests and responses (see section 9) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | For example `http://otel-collector:4318` |

Command-line flags are listed in [README.md, section 11](README.md#11-command-line-options).

---

## 11. Threat model

| Attack | Mock sample | Stopped by | Result |
|---|---|---|---|
| "Ignore all previous instructions..." | `m15` | Strong pattern (layer 4) | quarantined, no LLM call |
| Instructions in white or 1px HTML text | `m16` | Hidden-text scan of the raw email (2, 4) | quarantined |
| Zero-width characters splitting keywords | `m17` | Normalization, then pattern (1, 4) | quarantined |
| Instructions planted in quoted thread history | `m18` | Raw scan; quotes stripped for the LLM (3, 4) | quarantined |
| Spoofed bank (DKIM fail, free mail, Reply-To) | `m19` | Sender signals (5) | quarantined |
| Business email compromise: "change beneficiary account" | `m20` | Strong pattern (4) | quarantined |
| Classic phishing link plus urgency | `m11` | LLM classifies `phishing_suspicious`; no reply allowed | quarantined |
| Soft manipulation ("act as my manager and waive it") | `m23` | Weak pattern: no auto reply | needs_review |
| Model invents an amount, link or promise | - (tests) | Grounding, URL and promise checks (8) | needs_review |
| Model states a payment status the email never gave ("is being processed") | m06 in a live run | Status-claim check (8) | needs_review |
| Model calls a tool that doesn't exist, or asks for another email | - (tests) | ToolSession refuses (7) | error returned to the model |
| Auto-reply loops | `m21` | `is_auto_or_self` | ignored |
| Follow-up in a thread already handled | `m22` | Thread state | needs_review |
| Email flood to burn the quota | - (tests) | `MAX_LLM_CALLS_PER_RUN` | run stops |

### Known limits

- The pattern checks are mostly in English, so non-English injections rely on layers 6-8.
- The LLM's confidence is its own estimate, so it is only one of several signals.
- PDF attachments (most bank statements) are not read, so extracted fields may be empty.
- Real requests to change bank details are quarantined on purpose. A person handles them from the review
  queue.
- `output/` holds masked but still personal data, and it is not cleaned up automatically.

---

## 12. Development (TDD)

Every module was built test-first (red -> green -> refactor), in this order: models -> guardrails -> state
-> mailbox -> tools -> llm -> telemetry/actions -> agent -> CLI.

- The test suite runs fully offline. IMAP and HTTP are faked, and a scripted `FakeLLM` plays the model.
- One end-to-end test checks that all 23 mock emails reach their expected status.
- `pytest -m live` (opt-in) calls a real LLM.
- Code style is checked with `ruff` (lint and format). GitHub Actions runs the linter and the tests on
  every push (`.github/workflows/ci.yml`).
