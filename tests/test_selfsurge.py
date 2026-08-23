import concurrent.futures
import hashlib
import json
import re
import unittest
from urllib.parse import unquote

from generate import ROOT, catalog_entries
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

        generated = {path.name for path in (ROOT / "modules").glob("*.sgmodule")}
        self.assertEqual(generated, {name for name, _ in entries})

        manifest = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["modules"],
            {f"modules/{name}": source_url for name, source_url in entries},
        )
        web_catalog = json.loads(
            (ROOT / "web" / "catalog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {item["file"] for item in web_catalog},
            {name for name, _ in entries},
        )
        self.assertTrue(
            all(
                item["url"]
                == (
                    "https://raw.githubusercontent.com/msdx321/"
                    f"SelfSurge/main/modules/{item['file']}"
                )
                for item in web_catalog
            )
        )
        for relative, source in manifest["resources"].items():
            content = (ROOT / relative).read_bytes()
            self.assertEqual(
                hashlib.sha256(content).hexdigest(), source["sha256"]
            )

        raw_prefix = (
            "https://raw.githubusercontent.com/msdx321/SelfSurge/main/"
        )
        for module in (ROOT / "modules").glob("*.sgmodule"):
            for url in re.findall(raw_prefix + r"[^,\s\"]+", module.read_text()):
                relative = unquote(url.removeprefix(raw_prefix))
                self.assertTrue((ROOT / relative).is_file(), url)


if __name__ == "__main__":
    unittest.main()
