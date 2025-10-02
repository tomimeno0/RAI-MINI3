"""SQLite migration runner used for Windows deployments."""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "server.log"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "apps.sqlite"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_TABLE = "_schema_migrations"


def get_logger() -> logging.Logger:
    """Return a configured logger that writes to the server log."""

    logger = logging.getLogger("server.init_db")
    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger


@dataclass(frozen=True)
class Migration:
    """Represents a SQL migration file on disk."""

    path: Path

    @property
    def identifier(self) -> str:
        return self.path.name

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


class DatabaseMigrator:
    """Apply SQL migrations sequentially keeping track of state."""

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.db_path = db_path
        self.migrations_dir = migrations_dir
        self.logger = logger or get_logger()

    def migrate(self) -> List[str]:
        """Apply pending migrations and return the identifiers executed."""

        if not self.migrations_dir.exists():
            raise FileNotFoundError(f"Migrations directory not found: {self.migrations_dir}")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        applied: List[str] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            self._ensure_migrations_table(conn)
            already_applied = set(self._get_applied_migrations(conn))

            for migration in self._discover_migrations():
                if migration.identifier in already_applied:
                    self.logger.info("Migration %s already applied", migration.identifier)
                    continue

                self.logger.info("Applying migration %s", migration.identifier)
                conn.executescript(migration.read())
                conn.execute(
                    f"INSERT INTO {MIGRATION_TABLE} (id) VALUES (?)",
                    (migration.identifier,),
                )
                conn.commit()
                applied.append(migration.identifier)

        if applied:
            self.logger.info("Applied %d migration(s)", len(applied))
        else:
            self.logger.info("Database already up to date")
        return applied

    def _discover_migrations(self) -> Sequence[Migration]:
        files = sorted(self.migrations_dir.glob("*.sql"))
        return [Migration(path=file) for file in files]

    def _ensure_migrations_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
                id TEXT PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

    def _get_applied_migrations(self, conn: sqlite3.Connection) -> Iterable[str]:
        cursor = conn.execute(f"SELECT id FROM {MIGRATION_TABLE} ORDER BY id")
        return [row[0] for row in cursor.fetchall()]


def apply_migrations(
    db_path: Path | None = None,
    migrations_dir: Path | None = None,
    logger: logging.Logger | None = None,
) -> List[str]:
    """Helper to run migrations programmatically."""

    resolved_db = Path(db_path or DEFAULT_DB_PATH)
    resolved_migrations = Path(migrations_dir or DEFAULT_MIGRATIONS_DIR)
    migrator = DatabaseMigrator(resolved_db, resolved_migrations, logger=logger)
    return migrator.migrate()


def _prompt_reset_confirmation(db_path: Path, logger: logging.Logger) -> bool:
    logger.warning("Reset requested for database %s", db_path)
    first = input("This will permanently delete the database. Type 'yes' to continue: ")
    if first.strip().lower() != "yes":
        logger.info("Reset aborted at first confirmation")
        return False

    second = input(f"Type the database filename ({db_path.name}) to confirm: ")
    if second.strip() != db_path.name:
        logger.info("Reset aborted: filename did not match")
        return False
    return True


def reset_database(db_path: Path, logger: logging.Logger) -> None:
    """Remove the SQLite database and its SQLite sidecar files."""

    if not db_path.exists():
        logger.info("Database %s not found; nothing to reset", db_path)
        return

    logger.info("Removing database %s", db_path)
    db_path.unlink()

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            logger.debug("Removing sidecar %s", sidecar)
            sidecar.unlink()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply SQLite migrations for RAI-MINI server")
    parser.add_argument(
        "--database",
        dest="database",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--migrations",
        dest="migrations",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory containing .sql migrations",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the database before applying migrations (requires double confirmation)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = get_logger()

    try:
        db_path = args.database.resolve()
        migrations_dir = args.migrations.resolve()

        if args.reset:
            if _prompt_reset_confirmation(db_path, logger):
                reset_database(db_path, logger)
            else:
                logger.warning("Reset cancelled by user")
                return 1

        apply_migrations(db_path=db_path, migrations_dir=migrations_dir, logger=logger)
        return 0
    except Exception as exc:  # pragma: no cover - defensive for CLI usage
        logger.exception("Migration failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
