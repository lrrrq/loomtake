"""Anonymous local tests; never connect to an external tunnel."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.mplus_mcp.tunnel import check_alias, local_environment, mcp_command, native_command, private_directory, secret_reference


class TunnelLauncherTests(unittest.TestCase):
    def test_double_click_reuses_existing_profile_and_still_checks_connects_and_reads_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launcher = root / 'python'
            log, profiles = root / 'calls', root / 'profiles'
            launcher.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
action = sys.argv[2]
with open(os.environ['MPLUS_TEST_LOG'], 'a') as output:
    output.write(action + '\\n')
if action == 'prepare':
    folder = pathlib.Path(sys.argv[sys.argv.index('--profile-dir') + 1])
    folder.mkdir(parents=True, exist_ok=True)
    profile = folder / 'mplus-engineering.yaml'
    with profile.open('x') as output:
        output.write('anonymous profile retained')
''')
            launcher.chmod(0o700)
            script = Path(__file__).resolve().parents[1] / 'scripts/mplus_mcp/连接ChatGPT.command'
            env = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'LANG', 'TMPDIR'}}
            env.update(MPLUS_MCP_PYTHON=str(launcher), MPLUS_TUNNEL_CLIENT='/usr/bin/true',
                       MPLUS_MCP_SNAPSHOT=str(root / 'anonymous.sqlite3'),
                       MPLUS_TUNNEL_PROFILE_DIR=str(profiles), MPLUS_TUNNEL_ID='tunnel_anonymous',
                       MPLUS_TUNNEL_KEY_FILE=str(root / 'anonymous.key'), MPLUS_TEST_LOG=str(log))
            for _ in range(2):
                result = subprocess.run(['/bin/zsh', str(script)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(log.read_text().splitlines(),
                             ['check', 'prepare', 'doctor', 'connect', 'status',
                              'check', 'doctor', 'connect', 'status'])
            self.assertEqual((profiles / 'mplus-engineering.yaml').read_text(), 'anonymous profile retained')

    def test_stdio_child_does_not_inherit_runtime_or_ambient_keys(self):
        with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": "ANON_RUNTIME", "OPENAI_API_KEY": "ANON_API",
                                     "OPENAI_ADMIN_KEY": "ANON_ADMIN", "PYTHONPATH": "ANON_IMPORT"}):
            env = local_environment()
        for key in ("CONTROL_PLANE_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "PYTHONPATH"):
            self.assertNotIn(key, env)

    def test_stdio_paths_round_trip_shell_metacharacters_as_literal_arguments(self):
        path = Path('/tmp/space 中文/$(not-a-command);snapshot.sqlite3')
        argv = shlex.split(mcp_command(path, Path('/tmp/audit space')))
        self.assertEqual(argv[argv.index('--snapshot') + 1], str(path))
        self.assertEqual(argv[argv.index('--audit-dir') + 1], '/tmp/audit space')
        self.assertIn('stdio', argv)

    def test_private_files_are_references_and_shared_or_symlink_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / 'runtime.key'
            key.write_text('ANON_RUNTIME_SECRET')
            key.chmod(0o600)
            with patch.object(Path, 'read_text', side_effect=AssertionError('must not read key')):
                self.assertEqual(secret_reference(key), 'file:' + str(key))
            link = Path(temp) / 'link'
            link.symlink_to(key)
            with self.assertRaises(ValueError): secret_reference(link)
            key.chmod(0o644)
            with self.assertRaises(ValueError): secret_reference(key)
            directory = Path(temp) / 'profiles'
            self.assertEqual(private_directory(directory), directory)
            directory.chmod(0o755)
            with self.assertRaises(ValueError): private_directory(directory)

    def test_native_connect_always_attaches_provided_tunnel(self):
        args = argparse.Namespace(action='connect', alias='mplus-test', profile_dir=Path('/tmp/profiles'),
                                  tunnel_id='tunnel_anonymous', snapshot=Path('/tmp/input.sqlite3'), audit_dir=Path('/tmp/audit'))
        command = native_command(args, '/tmp/tunnel-client', 'file:/tmp/private.key')
        self.assertEqual(command[:3], ['/tmp/tunnel-client', 'runtimes', 'connect'])
        self.assertEqual(command[command.index('--tunnel-id') + 1], 'tunnel_anonymous')
        self.assertEqual(command[command.index('--runtime-api-key') + 1], 'file:/tmp/private.key')
        self.assertNotIn('admin', command)

    def test_connect_preserves_alias_bound_to_another_tunnel(self):
        payload = '{"aliases":[{"alias":"mplus-test","tunnel_id":"tunnel_old"}]}'
        with patch('scripts.mplus_mcp.tunnel.subprocess.run', return_value=subprocess.CompletedProcess([], 0, payload, '')):
            with self.assertRaises(ValueError): check_alias('client', 'mplus-test', 'tunnel_new', {})
            check_alias('client', 'mplus-test', 'tunnel_old', {})
            check_alias('client', 'different-alias', 'tunnel_new', {})


if __name__ == '__main__': unittest.main()
