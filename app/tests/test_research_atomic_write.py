import json
from pathlib import Path
import pytest
from research.runner import write_atomic


def test_result_write_recovers_from_temporary_windows_reader_lock(tmp_path, monkeypatch):
    target = tmp_path / 'run.json'
    target.write_text('{"status":"running"}')
    original = Path.replace
    attempts = []

    def transient_lock(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError('temporary reader lock')
        return original(source, destination)

    monkeypatch.setattr(Path, 'replace', transient_lock)
    write_atomic(target, {'status': 'completed', 'results': [1, 2]})
    assert json.loads(target.read_text())['results'] == [1, 2]
    assert len(attempts) == 3
    assert not target.with_suffix('.tmp').exists()


def test_permanent_write_failure_preserves_previous_result(tmp_path, monkeypatch):
    target = tmp_path / 'run.json'
    target.write_text('{"status":"running"}')
    def locked(*args):
        raise PermissionError('locked')
    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr('research.runner.time.sleep', lambda _: None)
    with pytest.raises(PermissionError):
        write_atomic(target, {'status': 'completed'})
    assert json.loads(target.read_text())['status'] == 'running'
