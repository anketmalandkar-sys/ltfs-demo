import pytest

from email_agent import keystore

ENV = """# LLM
GEMINI_API_KEY=gem-secret
HF_TOKEN=
IMAP_USERNAME=me@x.com
export IMAP_PASSWORD="app pass"
"""


def answers(monkeypatch, *values):
    """getpass answers, in order; fails the test if asked more often."""
    queue = list(values)
    monkeypatch.setattr(keystore, "getpass", lambda text: queue.pop(0) if queue else pytest.fail(f"unexpected {text}"))


def test_save_and_load_round_trip_without_plaintext_on_disk(tmp_path):
    path = tmp_path / "keystore.enc"
    keystore.save(path, "pw", {"GEMINI_API_KEY": "super-secret-key"})
    assert keystore.load(path, "pw") == {"GEMINI_API_KEY": "super-secret-key"}
    assert "super-secret-key" not in path.read_text()


def test_wrong_password_is_a_clear_error_without_the_values(tmp_path):
    path = tmp_path / "keystore.enc"
    keystore.save(path, "pw", {"IMAP_PASSWORD": "app-pass"})
    with pytest.raises(SystemExit, match="Wrong keystore password or damaged keystore") as exc:
        keystore.load(path, "other")
    assert "app-pass" not in str(exc.value)


def test_damaged_keystore_is_a_clear_error(tmp_path):
    path = tmp_path / "keystore.enc"
    path.write_text("not a keystore")
    with pytest.raises(SystemExit, match="damaged keystore"):
        keystore.load(path, "pw")


def test_each_save_uses_a_new_salt(tmp_path):
    path = tmp_path / "keystore.enc"
    keystore.save(path, "pw", {"HF_TOKEN": "t"})
    first = path.read_text()
    keystore.save(path, "pw", {"HF_TOKEN": "t"})
    assert path.read_text() != first


def test_build_moves_secrets_into_the_keystore_and_writes_references(tmp_path):
    env_in, password = tmp_path / "plain.env", tmp_path / "pw"
    env_in.write_text(ENV)
    password.write_text("long-password\n")
    store, env_out = tmp_path / "image" / "keystore.enc", tmp_path / "image" / ".env"
    assert keystore.main(["build", str(env_in), str(password), str(store), str(env_out)]) == 0
    assert keystore.load(store, "long-password") == {"GEMINI_API_KEY": "gem-secret", "IMAP_PASSWORD": "app pass"}
    assert env_out.read_text() == (
        "# LLM\nGEMINI_API_KEY=keystore:GEMINI_API_KEY\nHF_TOKEN=\nIMAP_USERNAME=me@x.com\n"
        "IMAP_PASSWORD=keystore:IMAP_PASSWORD\n"
    )
    assert env_in.read_text() == ENV  # the original .env is never changed


def test_build_without_an_env_file_makes_an_empty_one(tmp_path):
    password = tmp_path / "pw"
    password.write_text("long-password")
    store, env_out = tmp_path / "keystore.enc", tmp_path / ".env"
    keystore.build(tmp_path / "missing.env", password, store, env_out)
    assert env_out.read_text() == "" and keystore.load(store, "long-password") == {}


def test_build_needs_a_password(tmp_path):
    (tmp_path / "plain.env").write_text(ENV)
    with pytest.raises(SystemExit, match="keystore password"):
        keystore.build(tmp_path / "plain.env", tmp_path / "no-pw", tmp_path / "k.enc", tmp_path / ".env")


def test_list_shows_names_only(tmp_path, monkeypatch, capsys):
    store, password = tmp_path / "keystore.enc", tmp_path / "pw"
    password.write_text("long-password")
    keystore.save(store, "long-password", {"HF_TOKEN": "hf-value"})
    monkeypatch.setenv("KEYSTORE_PATH", str(store))
    monkeypatch.setenv("KEYSTORE_PASSWORD_FILE", str(password))
    keystore.main(["list"])
    out = capsys.readouterr().out
    assert "HF_TOKEN" in out and "hf-value" not in out


def test_unlock_reads_the_file_or_asks_on_a_terminal(tmp_path, monkeypatch):
    (tmp_path / "pw").write_text("secret\n")  # trailing newline, as `echo` writes it
    assert keystore.unlock(tmp_path / "pw") == "secret"
    monkeypatch.setattr(keystore, "interactive", lambda: True)
    answers(monkeypatch, "typed")
    assert keystore.unlock(tmp_path / "missing") == "typed"
    monkeypatch.setattr(keystore, "interactive", lambda: False)
    with pytest.raises(SystemExit, match="keystore password"):
        keystore.unlock(tmp_path / "missing")
