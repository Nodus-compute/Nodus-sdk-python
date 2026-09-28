import importlib.util
from pathlib import Path


def test_release_verifier_cannot_inherit_live_test_credentials(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/verify-release.py'
    spec = importlib.util.spec_from_file_location('release_verifier', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('NODUS_E2E_BASE_URL', 'https://do-not-contact.invalid')
    monkeypatch.setenv('NODUS_E2E_API_KEY', 'sentinel-not-a-secret')
    monkeypatch.setenv('NODUS_API_KEY', 'sentinel-not-a-secret')
    monkeypatch.setenv('PYTHONPATH', '/not-the-release')
    env = module.isolated_environment(tmp_path)
    assert not any(key.upper().startswith('NODUS_') for key in env)
    assert 'PYTHONPATH' not in env
    assert env['HOME'] == str(tmp_path)
    assert env['USERPROFILE'] == str(tmp_path)


def test_installed_wheel_cli_uses_the_same_isolated_environment(monkeypatch, tmp_path):
    """A parent installation must not supply CLI subprocesses to the wheel suite."""
    import json
    import os

    script = Path(__file__).resolve().parents[1] / 'scripts/verify-release.py'
    spec = importlib.util.spec_from_file_location('release_verifier', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stale = tmp_path / 'different-sdk' / 'bin'
    monkeypatch.setenv('PATH', str(stale) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('VIRTUAL_ENV', str(stale.parent))
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs['env'].copy()))
        return module.subprocess.CompletedProcess(command, 0)

    def metadata(command, **kwargs):
        scripts = Path(command[0]).parent
        return json.dumps({'version': '0.7.1', 'module': str(scripts.parent / 'nodus' / '__init__.py')})

    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module.subprocess, 'check_output', metadata)
    monkeypatch.setattr(module.sys, 'argv', [str(script), '--wheel', str(tmp_path / 'fixture.whl')])
    assert module.main() == 0
    test_command, environment = commands[-1]
    scripts = Path(test_command[0]).parent
    assert environment['PATH'].split(os.pathsep)[0] == str(scripts)
    assert environment['VIRTUAL_ENV'] == str(scripts.parent)
