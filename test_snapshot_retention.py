"""Daily archive retention is lossless and keeps restore/export IDs compatible."""
import datetime
import gzip
import json
import tarfile

import pytest
import snapshot_manager as module
from test_snapshot import temp_snapshot_env


def fixed_clock(monkeypatch, day):
    now = datetime.datetime.fromisoformat(day + 'T16:00:00+08:00')
    monkeypatch.setattr(module, 'get_now', lambda: now)


def test_retention_boundary_and_protected_backups(temp_snapshot_env, monkeypatch):
    manager = temp_snapshot_env['mgr']
    fixed_clock(monkeypatch, '2026-01-01')
    old = manager.update_daily_snapshot()
    sid = old['meta']['snapshot_id']
    original = (module.SNAPSHOTS_DIR / (sid + '.json')).read_bytes()
    protected = [manager.create_snapshot(tag=tag)['meta']['snapshot_id']
                 for tag in ('manual', 'pre_restore', 'imported')]
    fixed_clock(monkeypatch, '2026-04-01')
    boundary = manager.update_daily_snapshot(date_str='2026-01-02')
    archived = module.SNAPSHOTS_DIR / (sid + '.json.gz')
    assert gzip.decompress(archived.read_bytes()) == original
    assert not (module.SNAPSHOTS_DIR / (sid + '.json')).exists()
    assert manager.get_snapshot(sid) == old
    assert (module.SNAPSHOTS_DIR / (boundary['meta']['snapshot_id'] + '.json')).exists()
    assert all((module.SNAPSHOTS_DIR / (key + '.json')).exists() for key in protected)
    items = {item['snapshot_id']: item for item in manager.list_snapshots()}
    assert items[sid]['archived'] is True
    assert items[sid]['filename'] == sid + '.json.gz'
    assert manager.archive_old_daily_snapshots() == 0
    with tarfile.open(manager.export_archive(sid)) as tar:
        assert json.load(tar.extractfile('snapshot.json')) == old
    assert manager.restore_snapshot(sid, backup_current=False)['success'] is True
    assert not (module.SNAPSHOTS_DIR / (sid + '.json')).exists()
    assert manager.delete_snapshot(sid) is True
    assert manager.get_snapshot(sid) is None


def test_failed_archive_preserves_source(temp_snapshot_env, monkeypatch):
    manager = temp_snapshot_env['mgr']
    fixed_clock(monkeypatch, '2026-01-01')
    old = manager.update_daily_snapshot()
    sid = old['meta']['snapshot_id']
    source = module.SNAPSHOTS_DIR / (sid + '.json')
    before = source.read_bytes()
    fixed_clock(monkeypatch, '2026-04-01')
    write = module._atomic_bytes
    def fail(path, data):
        if path.name.endswith('.json.gz'):
            raise OSError('disk full')
        write(path, data)
    monkeypatch.setattr(module, '_atomic_bytes', fail)
    # A daily snapshot remains successful even when maintenance cannot write.
    manager.update_daily_snapshot()
    assert source.read_bytes() == before
    assert manager.get_snapshot(sid) == old


def test_archive_symlink_is_rejected(temp_snapshot_env, monkeypatch, tmp_path):
    manager = temp_snapshot_env['mgr']
    fixed_clock(monkeypatch, '2026-01-01')
    sid = manager.update_daily_snapshot()['meta']['snapshot_id']
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private')
    (module.SNAPSHOTS_DIR / (sid + '.json.gz')).symlink_to(outside)
    fixed_clock(monkeypatch, '2026-04-01')
    with pytest.raises(ValueError, match='路径'):
        manager.archive_old_daily_snapshots()
    assert outside.read_bytes() == b'private'
    assert (module.SNAPSHOTS_DIR / (sid + '.json')).exists()
