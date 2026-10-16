import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.config import initialize_config
from dev_agent.dashboard import Dashboard, handler, runs
from dev_agent.workflow.persistence import Journal


def test_empty_read_does_not_initialize(tmp_path: Path) -> None:
    assert runs(tmp_path) == []
    assert not (tmp_path / '.dev-agent').exists()


def test_multiple_projects_and_private_config(tmp_path: Path) -> None:
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    Journal(a).create('ONE', 'private ticket text', 'secret provider config')
    Journal(b).create('TWO', 'text', '{}')
    dashboard = Dashboard([a, b, a])
    result = dashboard.overview()
    assert len(result['projects']) == 2
    assert result['projects'][0]['tickets'][0]['ticket_id'] == 'ONE'
    assert 'secret provider' not in json.dumps(result)
    assert 'private ticket' not in json.dumps(result)
    with pytest.raises(ValueError):
        dashboard.project('-1')
    with pytest.raises(ValueError):
        dashboard.project('../../')
    with pytest.raises(ValueError):
        dashboard.detail('0', 'TWO')


def test_symlink_database_rejected(tmp_path: Path) -> None:
    state = tmp_path / '.dev-agent'
    state.mkdir()
    (state / 'state.sqlite3').symlink_to(tmp_path / 'other')
    (tmp_path / 'other').touch()
    with pytest.raises(ValueError, match='Symlink'):
        runs(tmp_path)


def test_configured_test_success_failure_and_duplicate(tmp_path: Path) -> None:
    initialize_config(tmp_path)
    dashboard = Dashboard([tmp_path])
    with patch('dev_agent.dashboard.threading.Thread'):
        dashboard.test('0')
        with pytest.raises(ValueError, match='already running'):
            dashboard.test('0')
    dashboard._test('0', tmp_path, '/usr/bin/true', 5)
    assert dashboard.overview()['jobs']['0']['exit_code'] == 0
    with patch('dev_agent.dashboard.threading.Thread'):
        dashboard.test('0')
    dashboard._test('0', tmp_path, '/usr/bin/false', 5)
    assert dashboard.overview()['jobs']['0']['exit_code'] != 0
    assert not dashboard.overview()['jobs']['0']['running']


def test_http_auth_origin_and_no_arbitrary_commands(tmp_path: Path) -> None:
    dashboard = Dashboard([tmp_path])
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(dashboard))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def request(path: str, headers: dict[str, str], body: bytes | None = None) -> int:
        req = urllib.request.Request(url + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        auth = {'X-Dashboard-Token': dashboard.token}
        assert request('/', {}) == 200
        assert request('/api/projects', {}) == 403
        assert request('/api/projects', auth) == 200
        assert request('/api/projects', dict(auth, Host='evil.example')) == 403
        assert request('/api/test', auth, b'{"project":"0"}') == 403
        assert request('/api/test', dict(auth, Origin='http://evil.example'), b'{}') == 403
        with patch.object(dashboard, 'test') as test:
            assert request('/api/test', dict(auth, Origin=url), b'{"project":"0"}') == 202
            test.assert_called_once_with('0')
        assert request('/api/exec', dict(auth, Origin=url), b'{}') == 404
        assert request('/api/test', dict(auth, Origin=url), b'[]') == 400
        assert request('/api/test', dict(auth, Origin=url), b'x' * 1025) == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_dashboard_cli(tmp_path: Path) -> None:
    with patch('dev_agent.dashboard.serve') as serve:
        result = CliRunner().invoke(app, ['dashboard', '--project', str(tmp_path),
                                        '--port', '0', '--no-open'])
        assert result.exit_code == 0, result.output
        serve.assert_called_once_with([tmp_path], 0, False)


def test_ticket_detail_includes_timeline(tmp_path: Path) -> None:
    from subprocess import CompletedProcess

    journal = Journal(tmp_path)
    journal.create("DEMO-1", "Example", "{}")
    step = journal.begin("DEMO-1", "investigate")
    journal.finish(step, "FAILED", error="Example failure")
    with patch("dev_agent.dashboard.subprocess.run", return_value=CompletedProcess(
        [], 0, '{"ticket":"DEMO-1"}', ""
    )):
        detail = Dashboard([tmp_path]).detail("0", "DEMO-1")
    assert detail["steps"][0]["error"] == "Example failure"
    assert detail["steps"][0]["status"] == "FAILED"
