from __future__ import annotations

import unittest

from manufacturing_sim import __version__


class VersionTest(unittest.TestCase):
    def test_release_version(self) -> None:
        self.assertEqual(__version__, "0.6.0")


if __name__ == "__main__":
    unittest.main()
