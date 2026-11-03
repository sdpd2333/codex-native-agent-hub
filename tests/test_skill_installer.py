"""Exercise the real installer using a disposable checkout and skills directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is required for the Windows installer")
class SkillInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nah skill install ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        checkout = self.root / "checkout"
        (checkout / "scripts").mkdir(parents=True)
        self.script = checkout / "scripts" / "install-nah-skill.ps1"
        shutil.copyfile(ROOT / "scripts" / self.script.name, self.script)
        self.source = checkout / "skills" / "nah"
        shutil.copytree(ROOT / "skills" / "nah", self.source)
        shutil.copytree(ROOT / "skills" / "nah-delegate", checkout / "skills" / "nah-delegate")
        self.skills = self.root / "skills with spaces"
        self.target = self.skills / "nah"

    def install(self, *args, env=None, success=True):
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(self.script), *args], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_install_idempotent_update_and_preserve_extra_files(self):
        self.install("-SkillsDir", str(self.skills))
        self.assertEqual((self.target / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        manifest = self.target / ".nah-skill-install.json"
        self.assertEqual(json.loads(manifest.read_text())["owner"], "codex-native-agent-hub/nah")
        self.install("-SkillsDir", str(self.skills))
        extra = self.target / "my-notes.txt"
        extra.write_text("keep me")
        with (self.source / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nUpdated packaged instruction.\n")
        self.install("-SkillsDir", str(self.skills))
        self.assertEqual((self.target / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        self.assertEqual(extra.read_text(), "keep me")

    def test_customizations_abort_before_any_managed_file_is_changed(self):
        self.install("-SkillsDir", str(self.skills))
        metadata = self.target / "agents" / "openai.yaml"
        metadata.write_text("customized by the user")
        before = (self.target / "SKILL.md").read_bytes()
        with (self.source / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nNew packaged instructions.\n")
        self.install("-SkillsDir", str(self.skills), success=False)
        self.assertEqual((self.target / "SKILL.md").read_bytes(), before)
        self.assertEqual(metadata.read_text(), "customized by the user")

    def test_unmanaged_install_and_invalid_manifest_are_preserved(self):
        self.target.mkdir(parents=True)
        custom = self.target / "SKILL.md"
        custom.write_text("unmanaged skill")
        self.install("-SkillsDir", str(self.skills), success=False)
        (self.target / ".nah-skill-install.json").write_text("{}")
        self.install("-SkillsDir", str(self.skills), success=False)
        self.assertEqual(custom.read_text(), "unmanaged skill")

    def test_codex_home_default_and_source_directory_protection(self):
        codex_root = self.root / "codex home"
        self.install(env={**os.environ, "CODEX_HOME": str(codex_root)})
        self.assertTrue((codex_root / "skills" / "nah" / "SKILL.md").is_file())
        original = (self.source / "SKILL.md").read_bytes()
        self.install("-SkillsDir", str(self.source.parent), success=False)
        self.assertEqual((self.source / "SKILL.md").read_bytes(), original)

    def test_delegation_skill_has_its_own_ownership_and_preserves_customizations(self):
        self.install("-SkillsDir", str(self.skills), "-SkillName", "nah-delegate")
        target = self.skills / "nah-delegate"
        manifest = json.loads((target / ".nah-skill-install.json").read_text())
        self.assertEqual(manifest['owner'], 'codex-native-agent-hub/nah-delegate')
        self.assertIn('get_routing_context', (target / 'SKILL.md').read_text())
        (target / 'SKILL.md').write_text('User custom rules')
        self.install("-SkillsDir", str(self.skills), "-SkillName", "nah-delegate", success=False)
        self.assertEqual((target / 'SKILL.md').read_text(), 'User custom rules')


if __name__ == "__main__":
    unittest.main()
