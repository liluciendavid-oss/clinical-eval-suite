"""Regression coverage: verification must not consume or modify study data.

Run with: python -m unittest discover -s tests -p test_verify_isolation.py -v
Only synthetic temporary projects are used; no real scoring files are opened.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "verify_all", Path(__file__).resolve().parents[1] / "src" / "verify_all.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

PROTECTED = (
    "data/responses/raw/responses.jsonl",
    "data/responses/key.jsonl",
    "data/responses/manifest.json",
    "data/responses/score_panel.json",
    "data/scores/human_pass1.csv",
    "data/scores/human_pass2.csv",
    "data/stats/study.json",
    "results/study.md",
)


class VerificationIsolationTests(unittest.TestCase):
    def test_fast_and_full_run_inside_disposable_project(self):
        """Child processes see no study inputs; their writes never reach source."""
        for fast in (False, True):
            with self.subTest(fast=fast), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                (source / "src").mkdir()
                (source / "data").mkdir()
                (source / "data/questions.jsonl").write_text("{}\n", encoding="utf-8")
                for relative in PROTECTED:
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"SYNTHETIC PRIVATE STUDY SENTINEL\n")
                before = {p: ((source / p).read_bytes(), (source / p).stat().st_mtime_ns)
                          for p in PROTECTED}
                # The actual subprocess uses __file__, as every real gate does.
                probe = "\n".join([
                    "from pathlib import Path",
                    "import sys",
                    "root = Path(__file__).resolve().parents[1]",
                    "assert root == Path.cwd().resolve()",
                    f"assert root != Path({str(source)!r})",
                    f"protected = {PROTECTED!r}",
                    "if sys.argv[1] == 'initial':",
                    "    assert not any((root / p).exists() for p in protected)",
                    "    assert (root / 'data/questions.jsonl').read_text() == '{}\\n'",
                    "    for p in protected:",
                    "        path = root / p",
                    "        path.parent.mkdir(parents=True, exist_ok=True)",
                    "        path.write_text('generated in disposable project')",
                    "else:",
                    "    assert (root / protected[0]).read_text() == 'generated in disposable project'",
                    "print('ISOLATION_OK')",
                ])
                (source / "src/probe.py").write_text(probe, encoding="utf-8")
                gates = [("isolated child", ["src/probe.py", "initial"], "ISOLATION_OK", None)]
                render = [("render child", ["src/probe.py", "render"], "ISOLATION_OK", None)]
                copied = []
                copy2 = verify.shutil.copy2

                def checked_copy(src, dst):
                    relative = Path(src).relative_to(source).as_posix()
                    self.assertNotIn(relative, PROTECTED, "study input was read by copier")
                    copied.append((relative, Path(dst)))
                    return copy2(src, dst)

                output = io.StringIO()
                with mock.patch.object(verify, "ROOT", source), \
                        mock.patch.object(verify, "GATES", gates), \
                        mock.patch.object(verify, "RENDER_GATES", render), \
                        mock.patch.object(verify.shutil, "copy2", side_effect=checked_copy), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(verify.main(["--fast"] if fast else []), 0)
                self.assertIn(f"全部 {1 if fast else 2} 门通过", output.getvalue())
                self.assertEqual([p for p, _ in copied if p.startswith("data/")],
                                 ["data/questions.jsonl"])
                self.assertTrue(copied)
                self.assertTrue(all(not dst.exists() for _, dst in copied),
                                "temporary verification project was not cleaned up")
                after = {p: ((source / p).read_bytes(), (source / p).stat().st_mtime_ns)
                         for p in PROTECTED}
                self.assertEqual(before, after, "verification modified original study files")

    def test_failed_gate_still_cleans_up_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "src").mkdir()
            (source / "src/fail.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
            copies = []
            original = verify.prepare_verification_project

            def prepare(src, dst):
                copies.append(dst)
                original(src, dst)

            with mock.patch.object(verify, "ROOT", source), \
                    mock.patch.object(verify, "GATES", [("failure", ["src/fail.py"], "", None)]), \
                    mock.patch.object(verify, "prepare_verification_project", side_effect=prepare), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main(["--fast"]), 1)
            self.assertEqual(len(copies), 1)
            self.assertFalse(copies[0].exists())


if __name__ == "__main__":
    unittest.main()
