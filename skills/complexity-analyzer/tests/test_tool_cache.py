"""Cache routing and installer integrity checks without downloads."""
import importlib.util
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'lib'))
from cxmetrics import core, nodetools

spec = importlib.util.spec_from_file_location('cx_install_tools', SKILL / 'bin/install_tools.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_override_and_adapters_use_same_persistent_cache(monkeypatch, tmp_path):
    monkeypatch.setenv('SWARM_DATA_DIR', str(tmp_path / 'persistent'))
    expected = tmp_path / 'persistent/tools/complexity-analyzer'
    assert core.tools_dir() == expected
    import os
    import subprocess
    result = subprocess.check_output(
        [sys.executable, '-B', '-c',
         'import install_tools; from cxmetrics import nodetools; '
         'assert install_tools.TOOLS == nodetools.NODE_DIR.parent; print(install_tools.TOOLS)'],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(SKILL / 'bin')),
        text=True)
    assert Path(result.strip()) == expected
    assert SKILL not in core.tools_dir().parents


def test_default_cache(monkeypatch, tmp_path):
    monkeypatch.delenv('SWARM_DATA_DIR', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    if core.os.name == 'nt':
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
        expected = tmp_path / 'local/swarm/tools/complexity-analyzer'
    else:
        expected = tmp_path / '.local/share/swarm/tools/complexity-analyzer'
    assert core.tools_dir() == expected


def test_installer_rejects_asset_hash_mismatch(monkeypatch, tmp_path):
    manifest = {'rust-code-analysis': {'version': 'test', 'repo': 'example/tool', 'tag': 'vtest',
                'assets': {'test-platform': {'asset': 'test.tar.gz', 'asset_sha256': 'bad',
                                             'binary_sha256': 'bad', 'member': 'binary'}}}}
    monkeypatch.setattr(installer, 'TOOLS', tmp_path / 'tools')
    monkeypatch.setattr(installer, 'platform_key', lambda: 'test-platform')
    monkeypatch.setattr(installer, 'download', lambda *args: b'corrupted archive')
    assert installer.install_rca(manifest) == 1
    assert not (installer.TOOLS / installer.rca_binary_name()).exists()


def test_installer_rejects_binary_hash_mismatch(monkeypatch, tmp_path):
    import io
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('binary', b'corrupted binary')
    data = archive.getvalue()
    manifest = {'rust-code-analysis': {'version': 'test', 'repo': 'example/tool', 'tag': 'vtest',
                'assets': {'test-platform': {'asset': 'test.zip', 'asset_sha256': installer.sha256_bytes(data),
                                             'binary_sha256': 'bad', 'member': 'binary'}}}}
    monkeypatch.setattr(installer, 'TOOLS', tmp_path / 'tools')
    monkeypatch.setattr(installer, 'platform_key', lambda: 'test-platform')
    monkeypatch.setattr(installer, 'download', lambda *args: data)
    assert installer.install_rca(manifest) == 1
    assert not (installer.TOOLS / installer.rca_binary_name()).exists()


@pytest.mark.parametrize('node', [False, True], ids=['fallback', 'node'])
def test_fixture_with_installed_tools(node, tmp_path, monkeypatch):
    # CI never installs tools. These checks are available to users who installed them explicitly.
    if installer.find_rca() is None:
        pytest.skip('rust-code-analysis is not installed; no downloads in tests')
    if node and not nodetools.available()[0]:
        pytest.skip('pinned node tools are not installed')
    pytest.importorskip('tree_sitter')
    pytest.importorskip('tree_sitter_rust')
    pytest.importorskip('radon')
    pytest.importorskip('vulture')
    sys.path.insert(0, str(SKILL / 'bin'))
    import selftest
    if node:
        monkeypatch.delenv('CXM_NO_NODE', raising=False)
    else:
        monkeypatch.setenv('CXM_NO_NODE', '1')
    first = selftest.produce(selftest.FIXTURE, tmp_path / 'first', node=node)
    selftest.produce(selftest.FIXTURE, tmp_path / 'second', node=node)
    for filename in ('metrics.json', 'report.md'):
        assert (tmp_path / 'first' / filename).read_bytes() == (tmp_path / 'second' / filename).read_bytes()
    selftest.failures.clear()
    selftest.check_fixture(first, node, 'node' if node else 'nonode')
    assert not selftest.failures
    golden = selftest.GOLDEN if node else selftest.GOLDEN_NODUP
    assert (tmp_path / 'first/metrics.json').read_bytes() == golden.read_bytes()
