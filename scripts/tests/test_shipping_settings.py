"""SC5 — a shipping worker never needs the permission channel to edit or commit.

⚠️ **Bridged to the JS module rather than restated.** The settings live in
`server/shipping-settings.mjs` because the server applies them; a Python copy of
the allowlist here would pass while the module drifted, which is the failure this
test exists to catch. So it reads the real module through `node`.
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIPPING_MODULE = os.path.join(REPO, "server", "shipping-settings.mjs")


def shipping_settings(arg):
    """`shippingSettings(arg)` from the JS module, read through `node`."""
    src = ("import(process.argv[1]).then(m => process.stdout.write("
           "JSON.stringify(m.shippingSettings(JSON.parse(process.argv[2])))))")
    out = subprocess.run(["node", "--input-type=module", "-e", src,
                          SHIPPING_MODULE, json.dumps(arg)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is required to read the JS module")
class PreventionSettingsTest(unittest.TestCase):

    def test_prevention_settings(self):
        s = shipping_settings(True)
        self.assertEqual(s["permissions"]["defaultMode"], "acceptEdits")
        self.assertEqual(s["permissions"]["allow"],
                         ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)"])
        # The negative control: a non-shipping worker is spawned without them.
        self.assertIsNone(shipping_settings(False))
        self.assertIsNone(shipping_settings(None))


if __name__ == "__main__":
    unittest.main()
