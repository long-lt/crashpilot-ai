from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_FILE = Path(__file__).resolve().parents[2] / "data/crashpilot.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE.resolve()}"

# Ensure data directory exists
DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
@event.listens_for(engine, "connect")
def sqlite_defaults(connection, _record):
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL").fetchone()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import app.db.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # create_all skips indexes on tables already present in pre-upgrade databases.
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_issues_project ON issues(project_key)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_details_issue ON crash_details(issue_id, project_key)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_tasks_project ON tasks(project_key)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_tasks_cluster ON tasks(cluster_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_discovery_project ON discovery_cycles(project_key)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_proposals_project ON rule_proposals(project_key)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_rule_history_project ON rule_change_history(project_key)")
