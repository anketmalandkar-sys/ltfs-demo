# Security

This page explains how the agent keeps credentials and customer data safe. The full guardrail design
(prompt injection, spoofed senders, output checks) is in [ARCHITECTURE.md, section 5](ARCHITECTURE.md#5-guardrail-layers).

## Credentials

The agent uses up to four secrets: `GEMINI_API_KEY`, `HF_TOKEN`, `IMAP_PASSWORD` and `IMAP_OAUTH_TOKEN`.

| Rule | How |
|---|---|
| Never in Git | `.env` and `secrets/*` are in `.gitignore`. Only `.env.example` (empty values) and `secrets/.gitkeep` are committed. |
| Never in a Docker image | `.env` and `secrets/` are in `.dockerignore`. `config/*.env` profiles, which are built into the image, hold only server names and ports. |
| Not in environment variables in Docker | Secret files: `./secrets/` is mounted read-only at `/run/secrets/`. Environment variables would show up in `docker inspect`. The agent logs a warning (with the name, never the value) if a secret still comes from the environment. |
| Never printed | Settings store secrets as Pydantic `SecretStr`, and the IMAP/SMTP login objects hide the secret from their `repr`. |
| Never in telemetry | The Gemini key is sent in an HTTP header, not the URL, so it never appears in a trace. Error spans record only the error type. |
| Can be rotated without a restart | In watch mode, settings are re-read every cycle, so a new password or token in the secret file is picked up on the next connect. |

## Network

- IMAP and SMTP always use TLS, and the server certificate and host name are always verified
  (`ssl.create_default_context()`). Python's `imaplib` does not verify them by default.
- In Docker, only Grafana is published, and only on `127.0.0.1:3000`. Its default login is
  `admin` / `password`, which is fine for a local demo only. Set `GRAFANA_ADMIN_PASSWORD` in `.env` for
  anything else. Anonymous access and sign-up are turned off.

## Least privilege

- The AI has only three tools: read the one email it was given, submit a triage, submit a reply.
  It cannot send, forward, open links or see other emails.
- The mailbox is opened read-only when labels are off (`--no-apply-labels`), and emails always stay unread.
- Sending is on by default, but only clean replies (no guardrail flag) in allowed categories are sent,
  with a limit per run. `AUTO_SEND=false` turns sending off.
- The container runs as a non-root user on a read-only file system with all Linux capabilities dropped.

## Customer data

- Card, Aadhaar, PAN, OTP and long account numbers are masked before the AI sees the email.
- `output/` contains email metadata and drafts. It is in `.gitignore`, and it is not cleaned up
  automatically.
- `OTEL_CAPTURE_CONTENT=true` (on by default in Docker, for the demo) records full AI prompts and answers
  in Tempo and Loki. These contain customer email text. Set it to `false` for real data.

## Before you push

Check that no secret is about to be committed:

```bash
git status --ignored          # .env, secrets/* and output/ should be listed as ignored
git ls-files secrets          # should show only secrets/.gitkeep
```

If a secret was ever committed or shared, **revoke it and create a new one** (for example at
https://aistudio.google.com/apikey). Deleting it from the repository is not enough.

## Reporting a problem

If you find a security issue, please contact the repository owner directly rather than opening a public issue.
