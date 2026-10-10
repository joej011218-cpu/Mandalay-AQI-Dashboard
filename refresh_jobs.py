"""Coalesced background refresh with PostgreSQL coordination and durable status.

The status is durable, the daemon thread is not a durable job queue.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
from threading import Thread
from uuid import uuid4

from sqlalchemy import func, select, update, text
from sqlalchemy.dialects.postgresql import insert
import app as dashboard

REFRESH_LOCK_ID = 219588960893  # Distinct from dataset and MQTT locks.
SCHEMA_LOCK_ID = 219588960894  # Shared by additive init-db and refresh migration.
STATE_KEY = 'mandalay'
db = dashboard.db


class RefreshState(db.Model):
    __tablename__ = 'mandalay_refresh_state'
    name = db.Column(db.String(20), primary_key=True)
    job_id = db.Column(db.String(36), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(10), nullable=False)
    requested_at = db.Column(db.DateTime(timezone=True), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    imported_rows = db.Column(db.Integer)
    error = db.Column(db.String(100))


def utcnow():
    return datetime.now(timezone.utc)


@contextmanager
def migration_connection():
    """Serialize additive DDL across releases/processes; fail closed on timeout.

    PostgreSQL transactional DDL and a transaction advisory lock ensure both
    the schema change and lock are committed or rolled back together.
    """
    with db.engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        conn.execute(select(func.pg_advisory_xact_lock(SCHEMA_LOCK_ID)))
        yield conn


def migrate_refresh_schema():
    with migration_connection() as conn:
        RefreshState.__table__.create(conn, checkfirst=True)
        # checkfirst does not repair a partially/incompatibly created table.
        # Verify required columns are readable before allowing startup.
        conn.execute(select(RefreshState.__table__).limit(0))


def release(conn):
    # Pool rollback does not release session advisory locks. Always unlock before
    # returning the connection to the pool; invalidate on any cleanup failure.
    try:
        conn.execute(select(func.pg_advisory_unlock(REFRESH_LOCK_ID)))
    except Exception:
        conn.invalidate()
    finally:
        conn.close()


def acquire():
    conn = db.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    try:
        if conn.execute(select(func.pg_try_advisory_lock(REFRESH_LOCK_ID))).scalar():
            return conn
        conn.close()
        return None
    except Exception:
        conn.invalidate()
        conn.close()
        raise


def snapshot(conn):
    row = conn.execute(select(RefreshState.__table__).where(RefreshState.name == STATE_KEY)).mappings().first()
    if row is None:
        return dict(status='idle', job_id=None)
    return {key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items() if key != 'name'}


def change(conn, job_id, **values):
    conn.execute(update(RefreshState).where(RefreshState.name == STATE_KEY,
                                          RefreshState.job_id == job_id).values(**values))


def reserve(conn, source):
    job_id = str(uuid4())
    values = dict(name=STATE_KEY, job_id=job_id, status='queued', source=source,
                  requested_at=utcnow(), started_at=None, finished_at=None,
                  imported_rows=None, error=None)
    stmt = insert(RefreshState).values(**values)
    conn.execute(stmt.on_conflict_do_update(index_elements=['name'],
        set_={key: value for key, value in values.items() if key != 'name'}))
    return job_id


def run(app, conn, job_id):
    try:
        with app.app_context():
            try:
                started = utcnow()
                change(conn, job_id, status='running', started_at=started)
                app.logger.info('Mandalay refresh started job=%s started_at=%s', job_id, started.isoformat())
                count = dashboard.refresh()
                db.session.commit()  # refresh/upsert already commit atomically; safe for empty/future implementations.
                finished = utcnow()
                change(conn, job_id, status='succeeded', finished_at=finished, imported_rows=count, error=None)
                app.logger.info('Mandalay refresh completed job=%s completed_at=%s imported_rows=%s',
                                job_id, finished.isoformat(), count)
                return count
            except Exception as error:
                finished = utcnow()
                # Do not log exception bodies/tracebacks: they may contain secrets
                # or provider/database connection strings. Type is sufficient here.
                error_type = type(error).__name__[:100]
                app.logger.error('Mandalay refresh failed job=%s completed_at=%s error_type=%s',
                                 job_id, finished.isoformat(), error_type)
                try:
                    db.session.rollback()
                except Exception:
                    app.logger.error('Could not roll back refresh session job=%s', job_id)
                try:
                    change(conn, job_id, status='failed', finished_at=finished,
                           imported_rows=None, error=error_type)
                except Exception:
                    app.logger.error('Could not persist refresh failure status job=%s', job_id)
                raise
            finally:
                db.session.remove()
    finally:
        release(conn)


def background(app, conn, job_id):
    try:
        run(app, conn, job_id)
    except Exception:
        pass  # Already recorded and safely logged; never leak exception text from Thread.


def enqueue(app):
    conn = acquire()
    if conn is None:
        # A concurrent Gunicorn process or CLI job already owns the refresh.
        # Coalesce rather than launch another thread or wait for its completion.
        with db.engine.connect() as reader:
            state = snapshot(reader)
            # Owner may have acquired the lock but not yet reserved its record.
            # Never associate a new accepted request with an old completed job.
            return state.get('job_id') if state['status'] in ('queued', 'running') else None
    try:
        job_id = reserve(conn, 'http')
        Thread(target=background, args=(app, conn, job_id), daemon=True,
               name='mandalay-refresh').start()
        # Ownership passes exclusively to the thread: request must not close conn.
        return job_id
    except Exception:
        try:
            if 'job_id' in locals():
                change(conn, job_id, status='failed', finished_at=utcnow(), error='LaunchFailed')
        finally:
            release(conn)
        raise


def status():
    conn = acquire()
    if conn is None:
        with db.engine.connect() as reader:
            return snapshot(reader)
    try:
        state = snapshot(conn)
        if state['status'] in ('queued', 'running'):
            # No refresh owns the lock: a restart/crash lost this daemon thread.
            change(conn, state['job_id'], status='interrupted', finished_at=utcnow(), error='WorkerStopped')
            state = snapshot(conn)
        return state
    finally:
        release(conn)


def cli_refresh(app):
    conn = acquire()
    if conn is None:
        return None
    try:
        job_id = reserve(conn, 'cli')
    except Exception:
        release(conn)
        raise
    return run(app, conn, job_id)
