import concurrent.futures
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit

from selfsurge import (
    convert_lpx,
    fetch_bytes,
    fetch_text,
    resource_urls,
)
from quantumult import adapt_script, convert_qx
from upstreams import CATALOG_URL, TREES, discover, select, surge_module
from startupads import STARTUP_MODULE_NAME, STARTUP_MODULE_URL, configure_fallback, startup_module


ROOT = Path(__file__).parent
GENERATED_DIRECTORIES = (Path("modules"), Path("scripts"), Path("resources"))
PUBLISHED_PREFIX = "https://raw.githubusercontent.com/msdx321/SelfSurge/main/"
PUBLISHED_MODULE_PREFIX = PUBLISHED_PREFIX + "modules/"
YOUTUBE_MODULE_NAME = "YouTube_remove_ads.sgmodule"
YOUTUBE_MODULE_URL = (
    "https://raw.githubusercontent.com/Maasea/sgmodule/master/"
    "YouTube.Enhance.sgmodule"
)
YOUTUBE_REFERENCE_URL = (
    "https://raw.githubusercontent.com/ddgksf2013/Rewrite/master/"
    "AdBlock/YoutubeAds.conf"
)
YOUTUBE_LICENSE_URL = (
    "https://raw.githubusercontent.com/Maasea/sgmodule/master/LICENSE"
)
YOUTUBE_RESOURCE_PATHS = {
    (
        "https://raw.githubusercontent.com/Maasea/sgmodule/master/"
        "Script/Youtube/youtube.response.js"
    ): Path(
        "scripts/YouTube/YouTube_remove_ads/YouTube_remove_ads_response.js"
    ),
    (
        "https://raw.githubusercontent.com/Maasea/sgmodule/master/"
        "Script/Youtube/youtube.request.js"
    ): Path(
        "scripts/YouTube/YouTube_remove_ads/YouTube_remove_ads_request.js"
    ),
    YOUTUBE_LICENSE_URL: Path("resources/licenses/Maasea-sgmodule-LICENSE"),
}


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


def module_warnings(module: str) -> list[str]:
    messages = {
        "# Requires main-profile policy selection:":
            "代理规则未启用，请在 Surge 主配置中手动指定策略。",
        "# Invalid upstream Loon rule:":
            "部分上游规则无效，已跳过；具体规则见模块注释。",
        "# Unsupported Loon policy":
            "部分上游策略不受支持，相关规则已跳过；详情见模块注释。",
        "# Surge $argument 为字符串":
            "上游脚本可能需要修改后才能读取参数，请先查看模块注释。",
        "# Loon enable 参数":
            "开关是否生效取决于上游脚本，请确认脚本支持关闭选项。",
    }
    lines = [re.sub(r'^#!REQUIREMENT "[^"]*" ', "", line) for line in module.splitlines()]
    warnings = [
        message for prefix, message in messages.items()
        if any(line.startswith(prefix) for line in lines)
    ]
    if "[Panel]" in lines:
        warnings.append("面板不支持 Loon 的长按节点功能。")
    if any(
        re.search(r'(?:script-path\s*=\s*|data=")https://', line)
        and not re.search(r'(?:script-path\s*=\s*|data=")' + re.escape(PUBLISHED_PREFIX), line)
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ):
        warnings.append("部分资源无法镜像，仍使用上游链接，可能无法加载。")
    return warnings


def youtube_module(source: str) -> str:
    for url, relative in YOUTUBE_RESOURCE_PATHS.items():
        if url == YOUTUBE_LICENSE_URL:
            continue
        if url not in source:
            raise ValueError(f"YouTube module is missing dependency: {url}")
        source = source.replace(url, PUBLISHED_PREFIX + relative.as_posix())

    lines = source.splitlines()
    header_end = next(
        (index for index, line in enumerate(lines) if not line.startswith("#!")),
        len(lines),
    )
    header = lines[:header_end]
    metadata = _module_metadata("\n".join(header))
    if not metadata.get("name") or not metadata.get("desc"):
        raise ValueError("YouTube module is missing name or description")
    if "category" not in metadata:
        desc_index = next(
            index for index, line in enumerate(header) if line.startswith("#!desc=")
        )
        header.insert(desc_index + 1, "#!category=去广告")

    body = lines[header_end:]
    while body and not body[0]:
        body.pop(0)
    license_path = YOUTUBE_RESOURCE_PATHS[YOUTUBE_LICENSE_URL]
    notes = [
        "",
        f"# Source: {YOUTUBE_MODULE_URL}",
        f"# Selected via ddgksf2013: {YOUTUBE_REFERENCE_URL}",
        "# Modified by SelfSurge: added category metadata and mirrored script paths.",
        f"# License: Apache-2.0 {PUBLISHED_PREFIX}{license_path.as_posix()}",
        "",
    ]
    return "\n".join(header + notes + body).rstrip() + "\n"


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


def build() -> tuple[dict[Path, bytes], dict, list[dict]]:
    icons = json.loads((ROOT / "module-icons.json").read_text(encoding="utf-8"))
    selected, decisions, excluded = select(discover())
    resource_sources = set().union(*(
        resource_urls(candidate.text) for candidate in selected
        if candidate.format == "loon" and candidate.name != YOUTUBE_MODULE_NAME
    ))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        resources = dict(executor.map(_download_resource, sorted(resource_sources)))
    unavailable = {url for url, content in resources.items() if content is None}
    script_sources = resources.copy()
    files = {}
    module_sources = {}
    errors = []
    for candidate in selected:
        name, source_url = candidate.name, candidate.url
        try:
            if name == YOUTUBE_MODULE_NAME:
                source_url = YOUTUBE_MODULE_URL
                source = fetch_text(source_url)
                module = youtube_module(source)
                decisions[name].update(upstream="Maasea / ddgksf2013", format="surge",
                                       source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                                       reason="ddgksf2013 recommends Maasea's native Surge YouTube module")
                decisions[name]["alternatives"].append({"upstream": "Kelee", "url": candidate.url})
            elif candidate.format == "loon":
                module = convert_lpx(candidate.text, source_url=source_url,
                                     unavailable_resources=unavailable, script_sources=script_sources)
            elif candidate.format == "surge":
                module = surge_module(candidate)
            else:
                module = convert_qx(candidate.text, source_url)
        except ValueError as error:
            errors.append(f"{name}: {error}")
            continue
        relative = Path("modules", name)
        files[relative] = module.encode()
        module_sources[relative.as_posix()] = source_url
    general_source = fetch_bytes(STARTUP_MODULE_URL, user_agent="surge").decode("utf-8-sig")
    general = startup_module(general_source)
    files[Path("modules", STARTUP_MODULE_NAME)] = general.encode()
    module_sources[f"modules/{STARTUP_MODULE_NAME}"] = STARTUP_MODULE_URL
    decisions[STARTUP_MODULE_NAME] = {
        "upstream": "ddgksf2013", "format": "quantumult", "coverage": "general",
        "source_sha256": hashlib.sha256(general_source.encode()).hexdigest(),
        "reason": "full general coverage by default; exclude_apps disables rules for the app modules the user enables",
        "alternatives": [],
    }
    if errors:
        raise ValueError(f"{len(errors)} module(s) failed conversion; generated files unchanged:\n" + "\n".join(errors))

    # Mirror dependencies across all sources. Existing Kelee/YouTube paths stay
    # stable, while other resources use their URL hash to avoid name collisions.
    paths = {url: resource_path(url) for url in resources if url not in unavailable}
    paths.update(YOUTUBE_RESOURCE_PATHS)
    license_url = "https://raw.githubusercontent.com/fmz200/wool_scripts/main/LICENSE"
    paths[license_url] = Path("resources/licenses/fmz200-wool_scripts-LICENSE")
    dependency_pattern = r'(?:script-path\s*=\s*|data=")(https://[^",\s]+)'
    for content in files.values():
        active = "\n".join(line for line in content.decode().splitlines() if not line.lstrip().startswith("#"))
        for url in re.findall(dependency_pattern, active):
            if url.startswith(PUBLISHED_PREFIX) or url in paths or url in unavailable:
                continue
            suffix = PurePosixPath(urlsplit(url).path).suffix
            directory = "scripts/upstream" if suffix == ".js" else "resources/upstream"
            paths[url] = Path(directory, hashlib.sha256(url.encode()).hexdigest()[:16] + (suffix or ".txt"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        resources.update(executor.map(_download_resource, sorted(set(paths) - set(resources))))
    unavailable.update(url for url, content in resources.items() if content is None)
    mirrored_sources = {}
    for url, relative in paths.items():
        content = resources[url]
        if content is None:
            continue
        files[relative] = content
        mirrored_sources[relative.as_posix()] = {"url": url, "sha256": hashlib.sha256(content).hexdigest()}
    web_catalog = []
    exclusions = {}
    for relative in list(files):
        if relative.parent != Path("modules"):
            continue
        module = files[relative].decode()
        action = ""
        lines = []
        for line in module.splitlines():
            if line.startswith("# QX action: "):
                action = line.removeprefix("# QX action: ")
            elif action and "script-path=" in line:
                url = re.search(dependency_pattern, line).group(1)
                if url not in unavailable:
                    key = hashlib.sha256((url + action).encode()).hexdigest()[:16]
                    adapted = Path("scripts/adapted", key + ".js")
                    content = adapt_script(resources[url].decode("utf-8-sig"), action).encode()
                    files[adapted] = content
                    mirrored_sources[adapted.as_posix()] = {
                        "url": url, "sha256": hashlib.sha256(content).hexdigest(),
                        "upstream_sha256": hashlib.sha256(resources[url]).hexdigest(),
                        "transformation": "quantumult-done:" + action,
                    }
                    line = line.replace(url, PUBLISHED_PREFIX + adapted.as_posix())
                action = ""
            if line.startswith("#!raw-url="):
                line = "#!raw-url=" + PUBLISHED_MODULE_PREFIX + relative.name
            lines.append(line)
        module = "\n".join(lines) + "\n"
        for url, path in paths.items():
            if url not in unavailable:
                module = module.replace(url, PUBLISHED_PREFIX + quote(path.as_posix(), safe="/"))
        if relative.name == STARTUP_MODULE_NAME:
            app_modules = {path.name: files[path].decode() for path in files
                           if path.parent == Path("modules") and path.name != STARTUP_MODULE_NAME}
            module, exclusions = configure_fallback(module, app_modules, decisions)
        metadata = _module_metadata(module)
        if relative.name in icons and metadata.get("icon", "") in {
            "", "https://raw.githubusercontent.com/fmz200/wool_scripts/main/icons/gif/naisi-01.gif",
        }:
            entry = icons[relative.name]
            metadata["icon"] = entry["icon"]
            module = re.sub(r"(?m)^#!icon=.*\n", "", module)
            header, separator, body = module.partition("\n\n")
            module = header + "\n#!icon=" + entry["icon"] + separator + body
            decisions[relative.name]["icon_source"] = entry["source"]
        files[relative] = module.encode()
        if "alias_of" in decisions[relative.name]:
            continue
        icon = metadata.get("icon", "")
        web_catalog.append({
            "category": metadata.get("category", ""),
            "date": metadata.get("date", ""),
            "description": metadata.get("desc", ""),
            "file": relative.name,
            "icon": icon if urlsplit(icon).scheme == "https" else "",
            "name": metadata.get("name", relative.stem),
            "url": PUBLISHED_MODULE_PREFIX + relative.name,
            "upstream": decisions[relative.name]["upstream"],
            "kind": "general" if relative.name == STARTUP_MODULE_NAME else "individual",
            "warnings": module_warnings(module),
        })
    web_catalog.sort(key=lambda item: (item["kind"] != "general", item["file"].lower()))
    for item in web_catalog:
        if item["file"] in exclusions:
            item["general_exclusion_key"] = Path(item["file"]).stem
            item["general_exclusion_rules"] = exclusions[item["file"]]
    decisions[STARTUP_MODULE_NAME]["app_exclusions"] = exclusions
    manifest = {
        "catalog": CATALOG_URL,
        "upstreams": {"Kelee": CATALOG_URL, **TREES},
        "modules": module_sources,
        "selection": decisions,
        "excluded": excluded,
        "resources": mirrored_sources,
        "unavailable_resources": sorted(unavailable),
    }
    return files, manifest, web_catalog


def main() -> None:
    files, manifest, web_catalog = build()
    _write_generated(files)
    (ROOT / "sources.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (ROOT / "web" / "catalog.json").write_text(
        json.dumps(web_catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"generated {len(manifest['modules'])} modules and "
          f"{len(manifest['resources'])} resources; {len(manifest['unavailable_resources'])} unavailable")


if __name__ == "__main__":
    main()
