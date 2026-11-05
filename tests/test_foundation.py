import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FoundationTests(unittest.TestCase):
    def test_required_project_documents_exist(self):
        for name in ("README.md", "docs/architecture.md", "docs/plan.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_local_secret_files_are_ignored(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", rules)
        self.assertIn(".env.*", rules)


if __name__ == "__main__":
    unittest.main()
