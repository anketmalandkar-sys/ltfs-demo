"""Remembers which emails were handled, so re-runs and --watch never process one twice."""

import hashlib
import json
import os
from pathlib import Path

from .models import Email, Status

RETRYABLE = {Status.ERROR}
NO_THREAD = {Status.IGNORED, Status.ERROR}


def email_key(email: Email) -> str:
    if email.message_id:
        return email.message_id
    raw = f"{email.from_addr}|{email.date}|{email.subject}"
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


class State:
    """Processed email keys, seen threads and retry counts, stored in one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"done": [], "threads": [], "attempts": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"state file {self.path} is corrupt; fix or delete it") from exc

    def is_done(self, email: Email) -> bool:
        return email_key(email) in self.data["done"]

    def thread_seen(self, thread_id: str) -> bool:
        return bool(thread_id) and thread_id in self.data["threads"]

    def attempts(self, email: Email) -> int:
        return self.data["attempts"].get(email_key(email), 0)

    def record(self, email: Email, status: Status) -> None:
        key = email_key(email)
        if status in RETRYABLE:
            self.data["attempts"][key] = self.attempts(email) + 1
        else:
            self.data["done"].append(key)
            self.data["attempts"].pop(key, None)
        if status not in NO_THREAD and email.thread_id and email.thread_id not in self.data["threads"]:
            self.data["threads"].append(email.thread_id)
        self._save()

    def forget(self, email: Email) -> None:
        """Make an email (and its thread) look unprocessed again, e.g. for --only reruns."""
        key = email_key(email)
        self.data["done"] = [done for done in self.data["done"] if done != key]
        self.data["attempts"].pop(key, None)
        if email.thread_id in self.data["threads"]:
            self.data["threads"].remove(email.thread_id)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
