import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit

from selfsurge import (
    convert_lpx,
    fetch_bytes,
    fetch_lpx,
    fetch_text,
    plugin_url,
    resource_urls,
)


ROOT = Path(__file__).parent
CATALOG_URL = "https://hub.kelee.one/list.json"
GENERATED_DIRECTORIES = (Path("modules"), Path("scripts"), Path("resources"))
PUBLISHED_MODULE_PREFIX = (
    "https://raw.githubusercontent.com/msdx321/SelfSurge/main/modules/"
)


def catalog_entries() -> list[tuple[str, str]]:
    payload = json.loads(fetch_text(CATALOG_URL))
    entries = []
    names = set()
    for item in payload.get("lists", []):
        source_url = plugin_url(item["url"])
        source_path = PurePosixPath(urlsplit(source_url).path)
        if source_path.suffix.lower() != ".lpx":
            raise ValueError(f"catalog entry is not an LPX plugin: {source_url}")
        name = source_path.stem + ".sgmodule"
        if name in names:
            raise ValueError(f"duplicate module filename: {name}")
        names.add(name)
        entries.append((name, source_url))
    if not entries:
        raise ValueError("Hub catalog contains no LPX plugins")
    return entries


def resource_path(url: str) -> Path:
    parsed = urlsplit(url)
    prefix = "/Resource/"
    if parsed.scheme != "https" or parsed.netloc != "kelee.one":
        raise ValueError(f"resource is not hosted by kelee.one: {url}")
    relative = PurePosixPath(parsed.path.removeprefix(prefix))
    if not parsed.path.startswith(prefix) or ".." in relative.parts:
        raise ValueError(f"invalid resource path: {url}")
    if relative.parts[0] == "JavaScript":
        return Path("scripts", *relative.parts[1:])
    return Path("resources", *relative.parts)


def _download_resource(url: str) -> tuple[str, bytes | None]:
    try:
        return url, fetch_bytes(url)
    except HTTPError as error:
        if error.code == 404:
            return url, None
        raise


def _module_metadata(module: str) -> dict[str, str]:
    metadata = {}
    for line in module.splitlines():
        if not line.startswith("#!"):
            continue
        key, separator, value = line[2:].partition("=")
        if separator:
            metadata[key] = value
    return metadata


def _write_generated(files: dict[Path, bytes]) -> None:
    expected = set(files)
    for directory in GENERATED_DIRECTORIES:
        root = ROOT / directory
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file() and path.relative_to(ROOT) not in expected:
                    path.unlink()

    for relative, content in files.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    for directory in GENERATED_DIRECTORIES:
        root = ROOT / directory
        if root.exists():
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass


def main() -> None:
    entries = catalog_entries()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        sources = dict(
            zip(
                (url for _, url in entries),
                executor.map(fetch_lpx, (url for _, url in entries)),
            )
        )

    resource_sources = set().union(
        *(resource_urls(source) for source in sources.values())
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        resources = dict(executor.map(_download_resource, sorted(resource_sources)))
    unavailable = {url for url, content in resources.items() if content is None}

    files = {}
    web_catalog = []
    module_sources = {}
    for name, source_url in entries:
        relative = Path("modules", name)
        module_sources[relative.as_posix()] = source_url
        module = convert_lpx(
            sources[source_url],
            source_url=source_url,
            unavailable_resources=unavailable,
        )
        files[relative] = module.encode()
        metadata = _module_metadata(module)
        icon = metadata.get("icon", "")
        if urlsplit(icon).scheme != "https":
            icon = ""
        web_catalog.append(
            {
                "category": metadata.get("category", ""),
                "date": metadata.get("date", ""),
                "description": metadata.get("desc", ""),
                "file": name,
                "icon": icon,
                "name": metadata.get("name", Path(name).stem),
                "url": PUBLISHED_MODULE_PREFIX + name,
            }
        )

    mirrored_sources = {}
    for url, content in resources.items():
        if content is None:
            continue
        relative = resource_path(url)
        files[relative] = content
        mirrored_sources[relative.as_posix()] = {
            "url": url,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    _write_generated(files)
    manifest = {
        "catalog": CATALOG_URL,
        "modules": module_sources,
        "resources": mirrored_sources,
        "unavailable_resources": sorted(unavailable),
    }
    (ROOT / "sources.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (ROOT / "web" / "catalog.json").write_text(
        json.dumps(web_catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"generated {len(module_sources)} modules and "
        f"{len(mirrored_sources)} resources; {len(unavailable)} unavailable"
    )


if __name__ == "__main__":
    main()
