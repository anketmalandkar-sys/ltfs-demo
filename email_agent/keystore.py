"""Encrypted keystore for the secrets in the Docker image.

Your .env keeps the real values. While the image is built (docker build, via ./start.sh), `build` reads that .env
and the keystore password as build secrets, puts the secrets (NAMES) into an encrypted keystore in the image and
writes the image's .env with references instead, e.g. HF_TOKEN=keystore:HF_TOKEN. At runtime the agent looks the
references up (main.resolve_references), with the password from a Docker secret.

python -m email_agent.keystore build ENV_IN PASSWORD_FILE KEYSTORE ENV_OUT   # run by the Dockerfile
python -m email_agent.keystore list                                           # names of the stored secrets

The keystore is JSON: a random salt and a Fernet token (AES + HMAC). The key comes from the password via scrypt.
"""

import argparse
import base64
import json
import os
import re
import secrets
import sys
from getpass import getpass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from dotenv import dotenv_values

NAMES = ("GEMINI_API_KEY", "HF_TOKEN", "IMAP_PASSWORD", "IMAP_OAUTH_TOKEN")
PREFIX = "keystore:"
VERSION = 1
START_HINT = "start with ./start.sh (Windows: .\\start.ps1)"


def is_reference(value: str) -> bool:
    return value.startswith(PREFIX)


def _fernet(password: str, salt: bytes) -> Fernet:
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def load(path: Path, password: str) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"Keystore {path} not found; {START_HINT}")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        salt = base64.b64decode(stored["salt"])
        return json.loads(_fernet(password, salt).decrypt(stored["data"].encode()))
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise SystemExit(
            f"Wrong keystore password or damaged keystore: {path}. Changed keystore_password? "
            "Run ./start.sh to rebuild the image with it"
        ) from None


def save(path: Path, password: str, values: dict[str, str]) -> None:
    """A new salt on every save."""
    salt = secrets.token_bytes(16)
    token = _fernet(password, salt).encrypt(json.dumps(values).encode()).decode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": VERSION, "salt": base64.b64encode(salt).decode(), "data": token}))


def keystore_path() -> Path:
    return Path(os.environ.get("KEYSTORE_PATH") or "keystore.enc")


def password_path() -> Path:
    return Path(os.environ.get("KEYSTORE_PASSWORD_FILE") or "keystore_password")


def read_password(path: Path) -> str:
    path = Path(path)
    password = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not password:
        raise SystemExit(f"The keystore password file {path} is missing or empty; {START_HINT}")
    return password


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def unlock(password_file: Path) -> str:
    """The password from the file (Docker secret), or typed in when there is no file and someone is at the terminal."""
    if Path(password_file).is_file() or not interactive():
        return read_password(password_file)
    return getpass("Keystore password: ")


def build(env_in: Path, password_file: Path, store: Path, env_out: Path) -> None:
    """Secrets from env_in go into the keystore; env_out is env_in with a reference in place of each secret."""
    env_in, env_out = Path(env_in), Path(env_out)
    password = read_password(password_file)
    text = env_in.read_text(encoding="utf-8") if env_in.is_file() else ""
    values = dotenv_values(env_in) if text else {}
    moved = {name: values[name] for name in NAMES if values.get(name) and not is_reference(values[name])}
    save(store, password, moved)
    for name in moved:
        text = re.sub(rf"^[ \t]*(export[ \t]+)?{name}[ \t]*=.*$", f"{name}={PREFIX}{name}", text, count=1, flags=re.M)
    env_out.parent.mkdir(parents=True, exist_ok=True)
    env_out.write_text(text, encoding="utf-8")
    print(f"Keystore built with: {', '.join(moved) or 'no secrets'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The encrypted keystore for the agent's secrets.")
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build", help="make the keystore and the referencing .env (Docker build)")
    for name in ("env_in", "password_file", "keystore", "env_out"):
        build_cmd.add_argument(name, type=Path)
    commands.add_parser("list", help="show which secrets are stored (names only)")
    args = parser.parse_args(argv)

    if args.command == "build":
        build(args.env_in, args.password_file, args.keystore, args.env_out)
    else:
        values = load(keystore_path(), read_password(password_path()))
        print("\n".join(sorted(values)) or "(empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
