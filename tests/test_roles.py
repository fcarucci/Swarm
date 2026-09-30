"""Role syntax stays consistent across Claude prompt tags and Codex task names."""
import unittest

import support  # noqa: F401 (sets sys.path)
from swarm import models
from swarm.hosts import get
from swarm.hosts.base import SpawnCall


class RoleSyntaxTests(unittest.TestCase):
    def test_explicit_codex_roles_and_legacy_names(self):
        host = get("codex")
        for name, expected in (
            ("product_manager__spec", "product_manager"),
            ("engineering_lead__architecture", "engineering_lead"),
            ("qa__e2e", "qa"),
            ("engineer__api", "engineer"),
            ("security_reviewer__audit", "security_reviewer"),
            ("judge_assistant__research", "judge_assistant"),
            ("verifier__acceptance", "verifier"),
            ("judge__final", "judge"),
            ("verifier-1", "verifier"),
            ("judge_1", "judge"),
            ("fixer", None),
            ("engineer__", None),
            ("__api", None),
            ("bad role__api", None),
            ("a" * 65 + "__api", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(host.spawn_role_hint(SpawnCall("", name)), expected)

    def test_invalid_prompt_roles_keep_default_model_role(self):
        for value in ("", "bad role", "UPPERCASE", "../judge", "a" * 65, "two__parts"):
            with self.subTest(value=value):
                self.assertEqual(models.role_of(f"[swarm role: {value}]", False), "worker")
                self.assertEqual(models.role_of(f"[swarm role: {value}]", True), "helper")


if __name__ == "__main__":
    unittest.main()
