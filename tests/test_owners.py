"""The names agents address a question for the person to: `[board] owner_names`, else git's user.name."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import support  # noqa: F401  (sets sys.path)
from swarm import owners


def git(name, rc=0):
    return mock.patch("swarm.owners.subprocess.run",
                      return_value=subprocess.CompletedProcess([], rc, stdout=name + "\n", stderr=""))


class OwnerNames(unittest.TestCase):
    def test_the_configured_list_wins_over_git(self):
        with git("Gitname"):
            self.assertEqual(owners.owner_names({"board": {"owner_names": ["Marigold", " Basil "]}}), ["Marigold", "Basil"])

    def test_a_single_string_is_one_name(self):
        self.assertEqual(owners.owner_names({"board": {"owner_names": "Marigold"}}), ["Marigold"])

    def test_empty_falls_back_to_git(self):
        with git("Gitname"):
            self.assertEqual(owners.owner_names({"board": {"owner_names": []}}), ["Gitname"])
            self.assertEqual(owners.owner_names({}), ["Gitname"])

    def test_no_git_name_means_none(self):
        with git("", rc=1):
            self.assertEqual(owners.owner_names({}), [])
        with mock.patch("swarm.owners.subprocess.run", side_effect=OSError):
            self.assertEqual(owners.owner_names({}), [])

    def test_names_that_would_break_the_stored_list_are_dropped(self):
        with git(""):
            self.assertEqual(owners.owner_names({"board": {"owner_names": ["a,b", 'q"x', "ok"]}}), ["ok"])
        with git("A, B"):
            self.assertEqual(owners.owner_names({}), [])

    def test_stored_names_are_split(self):
        self.assertEqual(owners.stored({"owner.names": "Marigold, Basil,"}), ["Marigold", "Basil"])
        self.assertEqual(owners.stored({}), [])
        self.assertEqual(owners.stored(None), [])

    def test_the_agents_brief_names_the_owner_and_the_configured_name(self):
        from swarm import hooks
        text = "\n".join(hooks._board_lines("Homer", "J", 200, {"board": {"owner_names": ["Marigold"]}}))
        self.assertIn("A question for the person behind this job (its owner) must be addressed `--to owner`", text)
        self.assertIn("`--to 'Marigold'`", text)
        with git(""):
            plain = "\n".join(hooks._board_lines("Homer", "J", 200, {}))
        self.assertIn("`--to owner` and end with `?`", plain)


if __name__ == "__main__":
    unittest.main()
