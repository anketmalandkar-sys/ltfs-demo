# Security

This page explains how the agent keeps credentials and customer data safe. The full guardrail design
(prompt injection, spoofed senders, output checks) is in [ARCHITECTURE.md, section 5](ARCHITECTURE.md#5-guardrail-layers).

## Credentials

The agent uses up to five secrets: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`, `IMAP_PASSWORD` and `IMAP_OAUTH_TOKEN`.

| Rule | How |
|---|---|
| Never in Git | `.env` is in `.gitignore`. Only `.env.example` (empty values) is committed. The keystore is built into the Docker image, never stored in the project. |
| No plain secret in the Docker image | `.env` is in `.dockerignore`. It reaches the build only as a build secret (never stored in a layer); the image gets an encrypted keystore and a `.env` with references. `config/*.env` profiles, which are built into the image, hold only server names and ports. |
| Only references in the image | The image's `.env` has secrets only as references (`HF_TOKEN=keystore:HF_TOKEN`). The values are in the encrypted keystore and are looked up at runtime (`main.resolve_references`). |
| Not in environment variables in Docker | The keystore password is a Docker secret (`/run/secrets/keystore_password`), not part of the image. Nothing secret is in the container environment. Environment variables would show up in `docker inspect`. The agent logs a warning (with the name, never the value) if a secret still comes from the environment. |
| Never printed | Settings store secrets as Pydantic `SecretStr`, and the IMAP/SMTP login objects hide the secret from their `repr`. |
| Never in telemetry | The Gemini key is sent in an HTTP header, not the URL, so it never appears in a trace. Error spans record only the error type. |
| Easy to rotate | Change the value in `.env` and run `./start.sh`: the image is rebuilt with the new keystore. |
| Encrypted at rest | The keystore is encrypted with AES (Fernet: AES-128-CBC + HMAC-SHA256). The key comes from the password via scrypt, with a new random salt on every save. `keystore list` shows only names. |

### What the keystore does and does not protect

The keystore keeps the secrets encrypted, so a copy of the image (for example in a registry) is useless
without the password. The password is in `keystore_password`, which you set and which is never committed or
copied into the image. It is still a file on this machine: anyone who can read it and the image can open the
keystore. On a real server, provide it from the platform's secret store (Docker Swarm or Kubernetes secrets, or
a vault). Do not push images built from a real `.env` to a public registry.

## Network

- IMAP and SMTP always use TLS, and the server certificate and host name are always verified
  (`ssl.create_default_context()`). Python's `imaplib` does not verify them by default.
- In Docker, only Grafana is published, and only on `127.0.0.1:3000`. Its default login is
  `admin` / `password`, which is fine for a local demo only. Set `GRAFANA_ADMIN_PASSWORD` in `.env` for
  anything else. Anonymous access and sign-up are turned off.

## Least privilege

- The AI sees only the one email it was given and has only two tools: submit a triage, submit a reply.
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
git status --ignored          # .env and output/ should be listed as ignored
git ls-files | grep -i env    # should show only .env.example and config/*.env
```

If a secret was ever committed or shared, **revoke it and create a new one** (for example at
https://aistudio.google.com/apikey). Deleting it from the repository is not enough.

## Reporting a problem

If you find a security issue, please contact the repository owner directly rather than opening a public issue.
