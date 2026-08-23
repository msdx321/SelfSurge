import concurrent.futures
import re
import unittest

from generate import catalog_entries
from selfsurge import convert_lpx, fetch_lpx


SECTIONS = {
    "[General]",
    "[Rule]",
    "[URL Rewrite]",
    "[Map Local]",
    "[Header Rewrite]",
    "[Body Rewrite]",
    "[Script]",
    "[MITM]",
}


class CatalogConversionTest(unittest.TestCase):
    def test_every_hub_plugin_converts(self) -> None:
        entries = catalog_entries()
        self.assertEqual(len(entries), len({name for name, _ in entries}))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            sources = executor.map(fetch_lpx, (url for _, url in entries))

            for (name, source_url), source in zip(entries, sources):
                module = convert_lpx(source, source_url=source_url)
                self.assertIn(f"# Source: {source_url}", module)
                self.assertNotIn("[Rewrite]", module)
                self.assertNotIn("[Argument]", module)
                self.assertNotIn("{{{{{", module)

                headings = {
                    line
                    for line in module.splitlines()
                    if line.startswith("[") and line.endswith("]")
                }
                self.assertLessEqual(headings, SECTIONS, name)

                declared = set()
                arguments = re.search(
                    r"^#!arguments=(.*)$", module, re.MULTILINE
                )
                if arguments:
                    declared = {
                        item.split(":", 1)[0]
                        for item in arguments.group(1).split(",")
                    }
                placeholders = set(
                    re.findall(r"\{\{\{([A-Za-z0-9_]+)\}\}\}", module)
                )
                self.assertLessEqual(placeholders, declared, name)


if __name__ == "__main__":
    unittest.main()
