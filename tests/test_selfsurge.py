import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import unittest
from urllib.parse import unquote

from generate import ROOT, catalog_entries
from selfsurge import _convert_rewrite, _surge_safe_jq, convert_lpx, fetch_lpx


SECTIONS = {
    "[General]",
    "[Rule]",
    "[URL Rewrite]",
    "[Map Local]",
    "[Header Rewrite]",
    "[Body Rewrite]",
    "[Script]",
    "[Panel]",
    "[MITM]",
}


class CatalogConversionTest(unittest.TestCase):
    def test_every_hub_plugin_converts(self) -> None:
        entries = catalog_entries()
        self.assertEqual(len(entries), len({name for name, _ in entries}))
        source_jq_count = 0
        converted_jq_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            sources = executor.map(fetch_lpx, (url for _, url in entries))

            for (name, source_url), source in zip(entries, sources):
                module = convert_lpx(source, source_url=source_url)
                self.assertIn(f"# Source: {source_url}", module)
                self.assertNotIn("[Rewrite]", module)
                self.assertNotIn("[Argument]", module)
                self.assertNotIn("{{{{{", module)
                self.assertNotIn("# Unsupported Loon policy", module)
                self.assertNotIn("omitted", module)
                self.assertNotRegex(module, r'argument="\[\{\{\{')
                source_generic_count = len(
                    re.findall(r"^generic\s+", source, re.MULTILINE)
                )
                self.assertEqual(
                    module.count("script-name="), source_generic_count, name
                )
                self.assertNotIn("img-url=", module)

                source_jq_count += len(
                    re.findall(
                        r"\b(?:request|response)-body-json-"
                        r"(?:jq|add|del|replace)\b",
                        source,
                    )
                )
                converted_jq_count += len(
                    re.findall(
                        r"^http-(?:request|response)-jq ",
                        module,
                        re.MULTILINE,
                    )
                )
                source_proxy_count = len(
                    re.findall(r"^[^#\n]+,\s*PROXY\s*$", source, re.MULTILINE)
                )
                self.assertEqual(
                    module.count("# Requires main-profile policy selection:"),
                    source_proxy_count,
                    name,
                )
                source_reject_count = len(
                    re.findall(
                        r"^[^#\n]+,\s*REJECT(?:-DROP)?(?:\s*(?:,|//|$))",
                        source,
                        re.MULTILINE,
                    )
                )
                self.assertEqual(
                    len(
                        re.findall(
                            r"^[^#\n]+,\s*REJECT-DROP(?:\s*(?:,|//|$))",
                            module,
                            re.MULTILINE,
                        )
                    ),
                    source_reject_count,
                    name,
                )

                lines = module.splitlines()
                self.assertTrue(lines[0].startswith("#!name="), name)
                header = []
                for line in lines:
                    if not line.startswith("#!"):
                        break
                    header.append(line)
                metadata = {
                    key: value
                    for line in header
                    for key, separator, value in [line[2:].partition("=")]
                    if separator
                }
                for key in ("name", "desc", "category"):
                    self.assertTrue(metadata.get(key), f"{name}: {key}")
                    self.assertEqual(
                        sum(line.startswith(f"#!{key}=") for line in header),
                        1,
                        f"{name}: duplicate {key}",
                    )
                if "[Map Local]" in module or "[Body Rewrite]" in module:
                    self.assertEqual(
                        metadata.get("requirement"), "CORE_VERSION>=20", name
                    )
                self.assertFalse(
                    any(line.startswith("#!") for line in lines[len(header) :]),
                    name,
                )

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

        self.assertEqual(converted_jq_count, source_jq_count)

        generated = {path.name for path in (ROOT / "modules").glob("*.sgmodule")}
        self.assertEqual(generated, {name for name, _ in entries})

        jq = shutil.which("jq")
        self.assertIsNotNone(jq, "jq is required to validate generated rewrites")
        jq_failures = []
        for path in (ROOT / "modules").glob("*.sgmodule"):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(
                    r"http-(?:request|response)-jq .* '(.*)'", line
                )
                if not match:
                    continue
                expression = match.group(1)
                self.assertEqual(expression, _surge_safe_jq(expression), path.name)
                result = subprocess.run(
                    [jq, "-n", f"if false then ({expression}) else . end"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    jq_failures.append(f"{path.name}: {result.stderr.strip()}")
        self.assertFalse(jq_failures, "\n".join(jq_failures))

        converted = _convert_rewrite(
            "^example response-body-json-del data[0] data[1] data[2]", {}
        )[0][1]
        expression = converted.rsplit(" '", 1)[1][:-1]
        result = subprocess.run(
            [jq, "-c", expression],
            input='{"data":[0,1,2,3,4,5]}',
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.stdout.strip(), '{"data":[1,3,5]}')

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
                item["name"] and item["description"] and item["category"]
                for item in web_catalog
            )
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
