"""Snapshot boundaries and failed restores; files live only under tmp_path."""
import copy
import io
import json
import tarfile

import pytest

import snapshot_manager as module
from strategy_rules import MomentumConfig
from test_snapshot import temp_snapshot_env


def persistent_bytes():
    return {path: path.read_bytes() for path in
            (module.SIM_ACCOUNT_PATH, module.STRATEGY_CONFIG_PATH, module.NOTIFICATION_CONFIG_PATH, module.INDEX_FILE)}


@pytest.mark.parametrize("bad", ["../outside", "/tmp/other", "a/b", "a\\b", "id.json", "index", "INDEX", "..", ""])
def test_ids_and_tags_cannot_traverse_or_overwrite_index(temp_snapshot_env, bad):
    manager = temp_snapshot_env["mgr"]
    before = persistent_bytes()
    for operation in (lambda: manager.get_snapshot(bad), lambda: manager.delete_snapshot(bad),
                      lambda: manager.export_archive(bad), lambda: manager.create_snapshot(snapshot_id=bad),
                      lambda: manager.create_snapshot(tag=bad)):
        with pytest.raises(ValueError):
            operation()
    assert persistent_bytes() == before


def test_existing_snapshot_symlink_is_never_followed(temp_snapshot_env, tmp_path):
    manager = temp_snapshot_env["mgr"]
    outside = tmp_path/"outside.json"
    outside.write_text("private outside contents")
    (module.SNAPSHOTS_DIR/"linked.json").symlink_to(outside)
    for operation in (lambda: manager.get_snapshot("linked"), lambda: manager.delete_snapshot("linked"),
                      lambda: manager.create_snapshot(snapshot_id="linked")):
        with pytest.raises(ValueError, match="路径"):
            operation()
    assert outside.read_text() == "private outside contents"


def test_public_creation_view_excludes_credentials_and_keeps_private_export(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    full = manager.create_snapshot()
    public = manager.public_view(full)
    assert public["meta"]["snapshot_id"] == full["meta"]["snapshot_id"]
    assert "notification_state" not in public and "account_state" not in public
    assert "AT_test123" not in json.dumps(public)
    assert manager.get_snapshot(full["meta"]["snapshot_id"])["notification_state"]["wxpusher_app_token"] == "AT_test123"


def test_same_second_backups_have_distinct_ids(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    first, second = manager.create_snapshot(tag="pre_restore"), manager.create_snapshot(tag="pre_restore")
    assert first["meta"]["snapshot_id"] != second["meta"]["snapshot_id"]


def test_http_string_restore_cannot_read_arbitrary_local_path(temp_snapshot_env):
    with pytest.raises(ValueError):
        temp_snapshot_env["mgr"].restore_snapshot(str(module.SIM_ACCOUNT_PATH))


def test_imported_metadata_id_is_validated_before_any_write(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    full = manager.create_snapshot()
    before = persistent_bytes()
    full["meta"]["snapshot_id"] = "../outside"
    full["account_state"]["summary"]["cash"] = 1
    with pytest.raises(ValueError):
        manager.restore_snapshot(full)
    assert persistent_bytes() == before


@pytest.mark.parametrize("name,kind", [
    ("../../outside.json", tarfile.REGTYPE), ("/tmp/outside.json", tarfile.REGTYPE),
    ("subdir/snapshot.json", tarfile.REGTYPE), ("snapshot.json", tarfile.SYMTYPE),
    ("snapshot.json", tarfile.LNKTYPE), ("snapshot.json", tarfile.FIFOTYPE),
])
def test_unsafe_tar_members_do_not_extract_or_change_state(temp_snapshot_env, tmp_path, name, kind):
    archive = tmp_path/"unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.REGTYPE:
            payload = b'{}'
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        else:
            member.linkname = "../../outside.json"
            tar.addfile(member)
    before = persistent_bytes()
    with pytest.raises(ValueError, match="不安全"):
        temp_snapshot_env["mgr"].import_archive_and_restore(archive)
    assert persistent_bytes() == before
    assert not (tmp_path/"outside.json").exists()


def test_duplicate_tar_members_are_rejected_before_restore(temp_snapshot_env, tmp_path):
    archive = tmp_path/"duplicate.tar.gz"
    full = temp_snapshot_env["mgr"].create_snapshot()
    payload = json.dumps(full).encode()
    with tarfile.open(archive, "w:gz") as tar:
        for _ in range(2):
            info = tarfile.TarInfo("snapshot.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    before = persistent_bytes()
    with pytest.raises(ValueError, match="重复"):
        temp_snapshot_env["mgr"].import_archive_and_restore(archive)
    assert persistent_bytes() == before


def test_invalid_parameters_are_preflighted_before_backup_or_writes(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    full = manager.create_snapshot()
    full["strategy_state"] = {"parameters": {"max_positions": 0}}
    manager.register_restore_validator(lambda data: MomentumConfig.from_dict(data["strategy_state"]["parameters"]))
    before = persistent_bytes()
    with pytest.raises(ValueError, match="max_positions"):
        manager.restore_snapshot(full, backup_current=True)
    assert persistent_bytes() == before


def test_cli_restore_rejects_invalid_momentum_parameters_without_server_hooks(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    full = manager.create_snapshot()
    full["strategy_state"] = {"active_strategy": "Momentum_Rotation_Pro", "parameters": {"max_positions": 0}}
    before = persistent_bytes()
    with pytest.raises(ValueError, match="max_positions"):
        manager.restore_snapshot(full, backup_current=False)
    assert persistent_bytes() == before


def changed_snapshot(manager):
    full = copy.deepcopy(manager.create_snapshot())
    full["meta"]["snapshot_id"] = "restore_trial"
    full["account_state"]["summary"]["cash"] = 123
    full["strategy_state"] = {"active_strategy": "Momentum_Rotation", "parameters": {"max_positions": 6}}
    full["notification_state"] = {"enabled": False, "wxpusher_app_token": "changed"}
    return full


def test_callback_rejection_rolls_back_all_files_and_memory(temp_snapshot_env):
    manager = temp_snapshot_env["mgr"]
    full = changed_snapshot(manager)
    before = persistent_bytes()
    memory = {}
    def reload(data):
        memory["cash"] = data["account_state"]["summary"]["cash"]
        if memory["cash"] == 123:
            raise ValueError("reload failed")
    manager.register_on_restore_callback(reload)
    with pytest.raises(ValueError, match="reload failed"):
        manager.restore_snapshot(full, backup_current=False)
    assert persistent_bytes() == before
    assert memory["cash"] == 80000
    assert not (module.SNAPSHOTS_DIR/"restore_trial.json").exists()


def test_late_write_failure_rolls_back_previously_written_files(temp_snapshot_env, monkeypatch):
    manager = temp_snapshot_env["mgr"]
    full = changed_snapshot(manager)
    before = persistent_bytes()
    original = module._atomic_json
    def fail_notification(path, value):
        if path == module.NOTIFICATION_CONFIG_PATH:
            raise OSError("disk full")
        original(path, value)
    monkeypatch.setattr(module, "_atomic_json", fail_notification)
    with pytest.raises(OSError, match="disk full"):
        manager.restore_snapshot(full, backup_current=False)
    assert persistent_bytes() == before
    assert not (module.SNAPSHOTS_DIR/"restore_trial.json").exists()
