# ============================================================
#   verify_secret_guard.py
#   Offline tests for the v2.2 secret handling. (Listed in the scanner's own
#   skip-list: the fake secrets below are the test data.)
#
#   Run:  python3 verify_secret_guard.py
# ============================================================

import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from core import secret_guard as sg      # noqa: E402

# built at runtime so this file never trips a naive scanner
FAKE_TG = "123456789" + ":" + "AAH" + "x" * 32
FAKE_SK = "sk-" + "proj" + "A1b2C3d4" * 3
FAKE_PW = "Hunter2!" + "zz"


class TestScan(unittest.TestCase):
    def kinds(self, text):
        return {k for _n, k, _e in sg.scan_text(text)}

    def test_the_original_leak_shape_is_caught(self):
        """MT5_PASSWORD = os.getenv("MT5_PASSWORD", "<real password>") — the
        harmless left-hand side must not hide the literal default."""
        line = f'MT5_PASSWORD = os.getenv("MT5_PASSWORD", "{FAKE_PW}")'
        self.assertIn("secret_literal_assignment", self.kinds(line))
        line = f'TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "{FAKE_TG}")'
        self.assertTrue(self.kinds(line))

    def test_dotenv_style_and_dict_style(self):
        self.assertTrue(self.kinds(f"MT5_PASSWORD={FAKE_PW}"))
        self.assertTrue(self.kinds(f'{{"AI_API_KEY": "{FAKE_SK}"}}'))

    def test_token_shapes(self):
        self.assertIn("telegram_bot_token", self.kinds(f"url = 'bot{FAKE_TG}/send'"))
        self.assertIn("openai_style_key", self.kinds(f"key = '{FAKE_SK}'"))
        self.assertIn("bearer_header",
                      self.kinds("Authorization: Bearer " + "a" * 30))

    def test_placeholders_and_references_are_not_flagged(self):
        clean = [
            "MT5_LOGIN=your_account_number",
            "MT5_PASSWORD=your_password",
            "TELEGRAM_TOKEN=your_telegram_bot_token",
            "AI_API_KEYS=sk-key-1,sk-key-2",
            'MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")',
            'MT5_LOGIN    = _int_env("MT5_LOGIN", 0)',
            'TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")',
            'if config.TELEGRAM_TOKEN in ("YOUR_BOT_TOKEN", "", None):',
            "bot = Bot(token=config.TELEGRAM_TOKEN)",
            "AI_POSITION_MAX_TOKENS=500",
            'AI_API_KEY = AI_API_KEYS[0] if AI_API_KEYS else ""',
            'AI_API_KEYS = [k.strip() for k in _ai_keys_raw.split(",") if k.strip()]',
            "password=config.MT5_PASSWORD,",
            # documentation prose with an elided example
            "Set `AI_PROVIDER=openai_compat` + `AI_API_KEYS=sk-...`.",
            '"AI_API_KEYS=sk-... in .env (or turn the flags off)"',
        ]
        for line in clean:
            self.assertEqual(sg.scan_text(line), [], line)

    def test_a_real_value_in_markdown_or_dotenv_is_still_caught(self):
        self.assertTrue(self.kinds(f"`MT5_PASSWORD={FAKE_PW}`"))
        self.assertTrue(self.kinds(f"export TELEGRAM_TOKEN={FAKE_TG}"))
        self.assertTrue(self.kinds(f"MT5_PASSWORD = '{FAKE_PW}'"))

    def test_the_telegram_url_form_is_caught(self):
        """Exception text reads https://api.telegram.org/bot<id>:<token>/sendMessage
        — the id is glued to the word 'bot'."""
        self.assertIn("telegram_bot_token", self.kinds(
            f"https://api.telegram.org/bot{FAKE_TG}/sendMessage"))

    def test_allow_marker_whitelists_a_line(self):
        self.assertEqual(
            sg.scan_text(f"MT5_PASSWORD={FAKE_PW}  # secret-scan: allow"), [])

    def test_excerpts_never_contain_the_secret(self):
        for _n, _k, excerpt in sg.scan_text(
                f'MT5_PASSWORD = os.getenv("MT5_PASSWORD", "{FAKE_PW}")\n'
                f"url = 'bot{FAKE_TG}/x'\nkey = '{FAKE_SK}'"):
            for secret in (FAKE_PW, FAKE_TG, FAKE_SK):
                self.assertNotIn(secret, excerpt)


class TestRedaction(unittest.TestCase):
    def test_generic_patterns_and_registered_values(self):
        sg.register_secret_values(FAKE_PW)
        out = sg.redact(f"login failed for pw {FAKE_PW} via bot{FAKE_TG}/getMe "
                        f"key {FAKE_SK}")
        for secret in (FAKE_PW, FAKE_TG, FAKE_SK):
            self.assertNotIn(secret, out)
        self.assertIn(sg.MASK, out)

    def test_log_filter_masks_message_args_and_tracebacks(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        lg = logging.getLogger("secret_guard_test")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg.addHandler(h)
        h.addFilter(sg.RedactingFilter())
        try:
            lg.error("Telegram error: %s", f"HTTPSConnectionPool(url=bot{FAKE_TG}/send)")
            try:
                raise RuntimeError(f"key {FAKE_SK} rejected")
            except RuntimeError:
                lg.exception("provider failed")
            lg.info("ordinary line stays readable: OPEN XAUUSD BUY lot 0.01")
        finally:
            lg.removeHandler(h)
        text = buf.getvalue()
        self.assertNotIn(FAKE_TG, text)
        self.assertNotIn(FAKE_SK, text)
        self.assertIn("ordinary line stays readable: OPEN XAUUSD BUY lot 0.01", text)
        self.assertIn("provider failed", text)

    def test_install_attaches_to_root_handlers_once(self):
        root = logging.getLogger()
        h = logging.StreamHandler(io.StringIO())
        root.addHandler(h)
        try:
            sg.install_log_redaction()
            sg.install_log_redaction()
            n = sum(isinstance(f, sg.RedactingFilter) for f in h.filters)
            self.assertEqual(n, 1)
        finally:
            root.removeHandler(h)


class TestRepoIsClean(unittest.TestCase):
    def test_tracked_files_have_no_secrets(self):
        """The scanner run over this very repo must come back clean."""
        tool = os.path.join(ROOT, "tools", "scan_secrets.py")
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        r = subprocess.run([sys.executable, tool, "--all"], capture_output=True,
                           text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_scanner_blocks_a_dirty_file_and_masks_the_output(self):
        tool = os.path.join(ROOT, "tools", "scan_secrets.py")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "leak.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(f'MT5_PASSWORD = os.getenv("MT5_PASSWORD", "{FAKE_PW}")\n')
            r = subprocess.run([sys.executable, tool, p], capture_output=True,
                               text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1)
        self.assertIn("BLOCKED", r.stdout)
        self.assertNotIn(FAKE_PW, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
