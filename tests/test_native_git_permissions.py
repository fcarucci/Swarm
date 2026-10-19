"""Real nested Codex Git capabilities in the existing role-aware launch jail (no model/API)."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_launch_isolation as isolation
from swarm.supervisor import launchhome


@unittest.skipUnless(isolation.CAN_JAIL and isolation.CODEX, 'needs native Codex and Linux user namespaces')
class NativeGitPermissions(isolation.LaunchHomeCase):
    def probe(self, layout, *, read_only, include_config=False, empty_include=False, conditional_include=False):
        repo = self.work / (layout + '-repo')
        self.git('init', '-q', str(repo))
        self.git('-C', str(repo), 'commit', '-q', '--allow-empty', '-m', 'base')
        workdir = repo
        if layout == 'linked':
            workdir = self.work / 'linked-worktree'
            self.git('-C', str(repo), 'worktree', 'add', '-q', str(workdir), '-b', 'native-author')
        sibling = self.work / 'unrelated-repo'
        self.git('init', '-q', str(sibling))
        (sibling / 'secret').write_text('must remain hidden')
        (workdir / 'owned-source.txt').write_text('owned exact source\n')
        gitdir, common = launchhome.git_dirs(self.cfg, str(workdir))
        included = None
        if include_config:
            included = Path(common) / 'approved-include'
            if empty_include != 'missing':
                included.write_text('' if empty_include else '[alias]\n\tshow-status = status\n')
            self.git('-C', str(workdir), 'config',
                     'includeIf.onbranch:never-active.path' if conditional_include else 'include.path',
                     './approved-include')
        protected = [str(Path(d) / name) for d in {gitdir, common}
                     for name in ('config', 'config.worktree', 'hooks/native-forbidden-hook')]
        if included:
            protected.append(str(included))
        if layout == 'linked':
            self.git('-C', str(repo), 'worktree', 'add', '-q', str(self.work / 'sibling-worktree'), '-b', 'other-owner')
            protected.append(str(Path(common) / 'worktrees/sibling-worktree/index'))
        code = '''import errno,json,os,subprocess
from pathlib import Path
from unittest import mock
out={}
for name,args in [('stage',['git','add','owned-source.txt']),('commit',['git','commit','-q','-m','native author capability'])]:
 r=subprocess.run(args,capture_output=True,text=True)
 out[name]={'rc':r.returncode,'err':r.stderr}
out['protected']={}
for path in PROTECTED:
 try:
  Path(path).write_text('must remain blocked')
 except OSError as error:
  out['protected'][path]=error.errno
 else:
  out['protected'][path]=None
out['rename_controls']={}
for path in PROTECTED:
 control=Path(path).parent if path.endswith('native-forbidden-hook') else Path(path)
 if not control.exists():
  continue
 try:
  control.rename(control.with_name(control.name+'-renamed'))
 except OSError as error:
  out['rename_controls'][str(control)]=error.errno
 else:
  out['rename_controls'][str(control)]=None
out['private_hidden']=not Path(PRIVATE).exists()
out['sibling_hidden']=not Path(SIBLING).exists()
out['replace_protected']={}
for path in PROTECTED:
 if path.endswith(('/config','/approved-include')):
  attacker=Path('replace-probe')
  try:
   attacker.write_text('must not replace protected config')
   os.replace(str(attacker),path)
  except OSError as error:
   out['replace_protected'][path]=error.errno
  else:
   out['replace_protected'][path]=None
out['unlink_protected']={}
for path in PROTECTED:
 if path.endswith(('/config','/approved-include')):
  try:
   os.unlink(path)
  except OSError as error:
   out['unlink_protected'][path]=error.errno
  else:
   out['unlink_protected'][path]=None
out['mounts']=[line for line in Path('/proc/self/mountinfo').read_text().splitlines() if WORKDIR in line]
print(json.dumps(out))
'''
        launch = layout + ('-reviewer' if read_only else '-engineer')
        h = launchhome.home_dir('J', launch)
        private = str(h / 'private-sentinel')
        code = 'PROTECTED=' + repr(protected) + ';PRIVATE=' + repr(private) + ';WORKDIR=' + repr(str(workdir)) + ';SIBLING=' + repr(str(sibling)) + '\n' + code
        # Use the ordinary native exec policy preparation, then its local sandbox command.
        # This runs no new agent and makes no model request.
        argv = launchhome.wrap(self.cfg, [isolation.CODEX, 'exec', '--skip-git-repo-check', '-'],
                               job='J', launch=launch, workdir=str(workdir), read_only=read_only)
        (h / 'private-sentinel').write_text('must remain hidden')
        argv = argv[:argv.index('--') + 1] + [isolation.CODEX, 'sandbox', '--', sys.executable, '-I', '-c', code]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), workdir

    def test_writer_own_clone_can_stage_commit_without_writing_protected_git_controls(self):
        out, workdir = self.probe('own', read_only=False)
        self.assertEqual(out['stage']['rc'], 0, out)
        self.assertEqual(out['commit']['rc'], 0, out)
        self.assertEqual(out['commit']['err'], '', out)
        self.assertTrue(out['private_hidden'])
        self.assertTrue(all(value is not None for value in out['rename_controls'].values()), out)
        self.assertTrue(out['sibling_hidden'])
        self.assertTrue(all(value is not None for value in out['replace_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['unlink_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)
        self.assertEqual(subprocess.check_output(['git', 'show', 'HEAD:owned-source.txt'], cwd=workdir, text=True),
                         'owned exact source\n')

    def test_writer_linked_worktree_can_stage_commit_without_writing_protected_git_controls(self):
        out, workdir = self.probe('linked', read_only=False)
        self.assertEqual(out['stage']['rc'], 0, out)
        self.assertEqual(out['commit']['rc'], 0, out)
        self.assertTrue(not out['commit']['err'] or
                        ('packed-refs.lock' in out['commit']['err'] and 'Read-only file system' in out['commit']['err']), out)
        self.assertEqual(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=workdir, text=True).strip(),
                         '')
        self.assertEqual(subprocess.check_output(['git', 'show', 'HEAD:owned-source.txt'], cwd=workdir, text=True),
                         'owned exact source\n')
        subprocess.run(['git', 'fsck', '--no-reflogs'], cwd=workdir, check=True, capture_output=True)
        self.assertTrue(out['private_hidden'])
        self.assertTrue(all(value is not None for value in out['rename_controls'].values()), out)
        self.assertTrue(out['sibling_hidden'])
        self.assertTrue(all(value is not None for value in out['replace_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['unlink_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)

    def test_reviewer_git_metadata_stays_read_only(self):
        out, workdir = self.probe('own', read_only=True)
        self.assertNotEqual(out['stage']['rc'], 0, out)
        self.assertNotEqual(out['commit']['rc'], 0, out)
        self.assertTrue(out['private_hidden'])
        self.assertTrue(all(value is not None for value in out['rename_controls'].values()), out)
        self.assertTrue(out['sibling_hidden'])
        self.assertTrue(all(value is not None for value in out['replace_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['unlink_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)

    def test_writer_cannot_change_existing_included_git_configuration(self):
        out, workdir = self.probe('own', read_only=False, include_config=True)
        self.assertEqual(out['commit']['rc'], 0, out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['replace_protected'].values()), out)
        self.assertTrue(all(value is not None for value in out['unlink_protected'].values()), out)

    def test_writer_cannot_change_empty_included_configuration(self):
        out, _ = self.probe('own', read_only=False, include_config=True, empty_include=True)
        self.assertEqual(out['commit']['rc'], 0, out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)

    def test_writer_cannot_create_inactive_missing_included_configuration(self):
        out, _ = self.probe('own', read_only=False, include_config=True,
                            empty_include='missing', conditional_include=True)
        self.assertEqual(out['commit']['rc'], 0, out)
        self.assertTrue(all(value is not None for value in out['protected'].values()), out)


class NativeGitProfileValidation(isolation.LaunchHomeCase):
    def test_linked_grants_do_not_include_common_root_or_sibling_admin(self):
        workdir = self.worktree()
        own, common = launchhome.git_dirs(self.cfg, workdir)
        writes, reads = launchhome.codex_git_permissions(self.cfg, workdir)
        self.assertIn(own, writes)
        self.assertNotIn(common, writes)
        self.assertIn(str(Path(common) / 'worktrees'), reads)
        self.assertTrue(all(path == own or path in [str(Path(common) / child)
                            for child in ('objects', 'refs', 'logs/refs')] for path in writes))

    def test_symlinked_git_storage_cannot_become_an_inner_write_grant(self):
        workdir = self.worktree()
        _, common = launchhome.git_dirs(self.cfg, workdir)
        refs = Path(common) / 'refs'
        refs.rename(refs.with_name('real-refs'))
        refs.symlink_to('real-refs', target_is_directory=True)
        with self.assertRaises(launchhome.LaunchIsolationError):
            launchhome.codex_git_permissions(self.cfg, workdir)

    def test_symlinked_included_configuration_is_rejected(self):
        workdir = self.worktree()
        _, common = launchhome.git_dirs(self.cfg, workdir)
        included = Path(common) / 'included'
        target = self.work / 'shared-config'
        target.write_text('[alias]\n\tshow-status = status\n')
        included.symlink_to(target)
        self.git('-C', workdir, 'config', 'include.path', str(included))
        with self.assertRaises(launchhome.LaunchIsolationError):
            launchhome.codex_git_permissions(self.cfg, workdir)

    def test_git_pointer_change_during_preparation_fails_closed(self):
        workdir = self.worktree()
        prepare = launchhome.prepare
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.git('-C', workdir, 'config', 'include.path', str(Path(workdir) / 'new-config'))
            (Path(workdir) / 'new-config').write_text('[alias]\n\tx = status\n')
            return result
        with mock.patch.object(launchhome, 'prepare', side_effect=changed):
            with self.assertRaisesRegex(launchhome.LaunchIsolationError, 'changed during launch'):
                launchhome.wrap(self.cfg, [isolation.CODEX or 'codex', 'exec', '--skip-git-repo-check', '-'],
                                job='J', launch='changed', workdir=workdir, probe=lambda: (True, ''))
