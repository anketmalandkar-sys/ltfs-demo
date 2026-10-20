import pytest

from email_agent.models import Email, Status
from email_agent.state import State, email_key


def email(**kw):
    data = dict(id="1", message_id="<m1@x>", thread_id="t1", from_addr="a@b.com", subject="Hi", date="d")
    data.update(kw)
    return Email(**data)


def test_email_key_uses_message_id_or_header_hash():
    assert email_key(email()) == "<m1@x>"
    no_id = email(message_id="")
    assert email_key(no_id) == email_key(no_id.model_copy()) != ""
    assert email_key(no_id) != email_key(no_id.model_copy(update={"subject": "Other"}))


def test_terminal_status_marks_done_and_thread_seen(tmp_path):
    state = State(tmp_path / "state.json")
    assert not state.is_done(email()) and not state.thread_seen("t1")
    state.record(email(), Status.DRAFTED)
    assert state.is_done(email()) and state.thread_seen("t1")


def test_ignored_email_does_not_mark_thread(tmp_path):
    state = State(tmp_path / "state.json")
    state.record(email(), Status.IGNORED)
    assert state.is_done(email()) and not state.thread_seen("t1")


def test_empty_thread_id_is_never_seen(tmp_path):
    state = State(tmp_path / "state.json")
    state.record(email(thread_id=""), Status.DRAFTED)
    assert not state.thread_seen("")


def test_error_counts_attempts_and_allows_retry(tmp_path):
    state = State(tmp_path / "state.json")
    state.record(email(), Status.ERROR)
    state.record(email(), Status.ERROR)
    assert not state.is_done(email())
    assert state.attempts(email()) == 2


def test_forget_removes_done_attempts_and_thread(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    state.record(email(), Status.DRAFTED)
    state.record(email(message_id="<m2@x>", thread_id="t2"), Status.ERROR)
    state.forget(email())
    state.forget(email(message_id="<m2@x>", thread_id="t2"))
    reloaded = State(path)
    assert not reloaded.is_done(email()) and not reloaded.thread_seen("t1")
    assert reloaded.attempts(email(message_id="<m2@x>", thread_id="t2")) == 0


def test_state_persists_atomically(tmp_path):
    path = tmp_path / "state.json"
    State(path).record(email(), Status.FILED)
    reloaded = State(path)
    assert reloaded.is_done(email()) and reloaded.thread_seen("t1")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_corrupt_state_file_fails_loudly(tmp_path):
    # Starting fresh would re-process (and re-draft) every email, so refuse instead.
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="state file"):
        State(path)
