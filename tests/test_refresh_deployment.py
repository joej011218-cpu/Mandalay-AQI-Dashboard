"""Deployment gates tested against real disposable PostgreSQL and shell startup."""
import os
from pathlib import Path
import subprocess
from threading import Event, Thread

import pytest
from sqlalchemy import func, inspect, select

from app import db, Reading
import refresh_jobs as jobs
from test_app import application, now_hour


def test_migration_waits_for_database_lock_and_preserves_data(application):
    db.session.add(Reading(timestamp=now_hour(), pm2_5=23, source='test-mandalay'))
    db.session.commit()
    jobs.RefreshState.__table__.drop(db.engine)
    started, finished = Event(), Event()
    errors = []

    def migrate():
        with application.app_context():
            started.set()
            try:
                jobs.migrate_refresh_schema()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

    thread = Thread(target=migrate)
    try:
        with db.engine.begin() as owner:
            owner.execute(select(func.pg_advisory_xact_lock(jobs.SCHEMA_LOCK_ID)))
            thread.start()
            assert started.wait(2)
            assert not finished.wait(0.2), 'Migration bypassed the database lock'
            assert not inspect(owner).has_table('mandalay_refresh_state')
    finally:
        # Always release the transaction lock and join before fixture teardown.
        if thread.ident is not None:
            thread.join(5)
    assert not thread.is_alive() and not errors
    jobs.migrate_refresh_schema()  # Repeated deployment is a no-op.
    assert db.session.scalar(select(func.count()).select_from(Reading)) == 1
    assert db.session.scalar(select(Reading.pm2_5)) == 23
    assert db.session.scalar(select(func.count()).select_from(jobs.RefreshState)) == 0


def test_failed_migration_rolls_back_ddl_and_releases_lock(application, monkeypatch):
    db.session.add(Reading(timestamp=now_hour(), source='test-mandalay'))
    db.session.commit()
    table = jobs.RefreshState.__table__
    table.drop(db.engine)
    create = table.create

    def fail_after_create(*args, **kwargs):
        create(*args, **kwargs)
        raise RuntimeError('simulated migration failure')

    monkeypatch.setattr(table, 'create', fail_after_create)
    with pytest.raises(RuntimeError):
        jobs.migrate_refresh_schema()
    assert not inspect(db.engine).has_table(table.name)
    assert db.session.scalar(select(func.count()).select_from(Reading)) == 1
    with db.engine.begin() as conn:
        assert conn.scalar(select(func.pg_try_advisory_xact_lock(jobs.SCHEMA_LOCK_ID)))
    monkeypatch.setattr(table, 'create', create)
    assert application.test_cli_runner().invoke(args=['migrate-refresh']).exit_code == 0


@pytest.mark.parametrize('migration_exit', [0, 42])
@pytest.mark.parametrize('outside_root', [False, True])
def test_startup_migrates_before_serving_and_fails_closed(tmp_path, migration_exit, outside_root):
    # Stub only executables, run the actual production startup script.
    log = tmp_path / 'commands'
    for name, body in {
        'flask': ('[ "$PWD" = "$EXPECTED_ROOT" ] || exit 87\n'
                  f'echo "flask $*" >> "$COMMAND_LOG"\nexit {migration_exit}\n'),
        'gunicorn': 'echo "gunicorn $*" >> "$COMMAND_LOG"\n',
    }.items():
        executable = tmp_path / name
        executable.write_text('#!/usr/bin/env bash\n' + body)
        executable.chmod(0o700)
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
           'COMMAND_LOG': str(log), 'PORT': '4321', 'EXPECTED_ROOT': str(root)}
    command = ['bash', str(root / 'scripts/start_web.sh') if outside_root else 'scripts/start_web.sh']
    result = subprocess.run(command, cwd=tmp_path if outside_root else root, env=env,
                            capture_output=True, text=True, timeout=10)
    commands = log.read_text().splitlines()
    assert commands[0] == 'flask --app wsgi migrate-refresh'
    assert result.returncode == migration_exit
    if migration_exit:
        assert len(commands) == 1
    else:
        assert commands[1] == ('gunicorn wsgi:app --bind 0.0.0.0:4321 '
                                '--workers 2 --timeout 60 --access-logfile -')
