"""Single-process container entrypoint with durable storage and private personas."""
from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .config import Settings
from .persona_context import load_persona


def prepare_persona(directory: Path) -> None:
    """Materialize the private deployment secret without normalizing file bytes."""
    encoded = os.getenv("SWARMBOARD_PERSONA_BUNDLE_B64")
    if encoded:
        try:
            if len(encoded) > 1_200_000:
                raise ValueError("oversized bundle")
            bundle = json.loads(base64.b64decode(encoded, validate=True))
            if not isinstance(bundle, dict) or set(bundle) != {"AGENTS.md", "memory.md"}:
                raise ValueError("invalid files")
            if any(not isinstance(value, str) for value in bundle.values()):
                raise ValueError("invalid contents")
            contents = {name: value.encode("utf-8") for name, value in bundle.items()}
            if any(not value.strip() or len(value) > 400_000 for value in contents.values()):
                raise ValueError("invalid size")
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise ValueError("SWARMBOARD_PERSONA_BUNDLE_B64 must encode both complete UTF-8 persona files") from None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, content in contents.items():
            path = directory / name
            # Every turn reads the captured DB snapshot, so deployment writes do
            # not change an ongoing session. Reload persona explicitly to adopt edits.
            with path.open("wb") as stream:
                os.chmod(path, 0o600)
                stream.write(content)
    load_persona(directory)  # Fail startup instead of serving a board without Ada.


@contextmanager
def storage_lock(database: Path):
    """Prevent accidental duplicate schedulers sharing the same database."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with database.with_suffix(database.suffix + ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Swarmboard already owns this database; run exactly one service instance and worker") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def backup_database(database: Path, destination: Path | None = None) -> Path | None:
    """Use SQLite's online backup API, including WAL data, before schema updates."""
    if not database.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = destination or database.parent / "backups" / f"swarmboard-{timestamp}.db"
    if target.exists() or target.resolve() == database.resolve():
        raise ValueError("Backup destination must be a new file")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.touch(mode=0o600, exist_ok=False)
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Database backup failed integrity validation")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def create_hosted_app(settings: Settings):
    from .app import create_app
    from .auth import AuthSettings
    from .codex_gateway import validate_codex_configuration
    from .harness import codex_model_source, persona_spec
    from .repository import Repository

    os.environ.setdefault("SWARMBOARD_REQUIRE_AUTH", "1")
    AuthSettings.from_env()
    validate_codex_configuration()
    prepare_persona(settings.persona_dir)
    app = create_app(settings=settings)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            with app.state.session_factory.begin() as session:
                repo = Repository(session)
                if not any(agent.handle in {"ada", "persona1"} for agent in repo.list_agents()):
                    spec = persona_spec(load_persona(settings.persona_dir), codex_model_source())
                    repo.create_agent(**spec.model_dump())
            yield

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    import uvicorn
    from sqlalchemy.engine import make_url

    settings = Settings.from_env()
    database = Path(make_url(settings.database_url).database).resolve()
    # The image's writable mount can be owned by the host. Initialize its owner
    # before dropping privileges; inference and the web server run unprivileged.
    if os.getuid() == 0:
        import pwd
        account = pwd.getpwnam("swarmboard")
        database.parent.mkdir(parents=True, exist_ok=True)
        os.chown(database.parent, account.pw_uid, account.pw_gid)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    with storage_lock(database):
        from .migration import import_database
        import_database(database)
        backup_database(database)
        app = create_hosted_app(settings)
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")),
                    workers=1, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
