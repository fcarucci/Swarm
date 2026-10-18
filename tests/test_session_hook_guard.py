"""Worker hook identity survives stripped shell environments for coordinator binding policy."""
from unittest import mock

from test_routing import RoutingEnv
from swarm import enrolment
from swarm.board.autoinit import store_key


class SessionHookGuardTests(RoutingEnv):
    def test_worker_payload_denies_literal_wrappers_even_when_environment_is_cleared(self):
        self.activate('J')
        self.spawn('a1', '[swarm job: J]\nwork')
        commands = ('env -i swarm session --job J --clear',
                    'sh -c "swarm session --job J --clear"',
                    'env -i python3 -B -m swarm.cli session --job J --clear',
                    'python3 -c \'from swarm import cli; cli.main(["session","--job","J","--clear"])\'')
        for command in commands:
            with self.subTest(command=command):
                out = self.hook('turn', agent_id='a1', tool_name='Bash', tool_input={'command': command})
                self.assertEqual((out or {}).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_private_self_enrolment_denies_without_payload_agent_id(self):
        self.activate('J')
        enrolment.write(store_key(self.cfg), job='J', agent_key='self-worker', harness='codex',
                        session_id='self-worker', cwd=str(self.tmp))
        out = self.hook('turn', agent_id=None, session='self-worker', host='codex', tool_name='Bash',
                        tool_input={'command': 'env -i swarm session --job J --clear'})
        self.assertEqual((out or {}).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_authorized_root_payload_can_reassign(self):
        self.activate('J')
        out = self.hook('turn', agent_id=None, tool_name='Bash',
                        tool_input={'command': 'swarm session --job J --clear'})
        self.assertNotEqual((out or {}).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
