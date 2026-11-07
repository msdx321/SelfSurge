"""Discover upstream plugins and select one implementation per application."""

import concurrent.futures
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from selfsurge import _surge_safe_jq, fetch_text, plugin_url
from quantumult import quote_body_field


CATALOG_URL = "https://hub.kelee.one/list.json"
TREES = {
    "fmz200": "https://api.github.com/repos/fmz200/wool_scripts/git/trees/main?recursive=1",
    "ddgksf2013": "https://api.github.com/repos/ddgksf2013/Rewrite/git/trees/master?recursive=1",
}
SOURCE_ORDER = {"Kelee": 0, "fmz200": 1, "ddgksf2013": 2}

# Different product names that cannot be inferred from filenames and titles.
# Editions and mini programs remain distinct unless their scope is the same.
ALIASES = {
    "小黄鱼": "闲鱼", "goofish": "闲鱼", "xianyu": "闲鱼",
    "小宇宙fm": "小宇宙", "网易大神": "neteasegodlike",
    "微信小程序": "wexinminiprograms", "applet": "wexinminiprograms",
    "微信公众号": "weixinofficialaccounts",
    "bingsimplify": "bing", "bing首页简化": "bing",
    "keep应用": "keep", "redditenhance": "reddit", "reddit增强": "reddit",
    "菜鸟裹裹": "菜鸟", "cainiaoguoguo": "菜鸟", "cainiao": "菜鸟",
    "优酷视频": "优酷", "youkuvideo": "youku",
    "萤石云视频": "萤石", "videogo": "ezviz",
    "小米音箱": "小爱音箱", "xiaomispeaker": "xiaoaispeaker",
}


@dataclass
class Candidate:
    name: str
    url: str
    upstream: str
    format: str
    title: str = ""
    adblock: bool = True
    text: str = ""


def _identity(value: str) -> str:
    value = re.sub(r"\[墨鱼版\]", "", value)
    value = re.sub(r"去除?广告|净化|_?remove_?ads|Ads$", "", value, flags=re.I)
    value = re.sub(r"[^\w]", "", value).replace("_", "").lower()
    return ALIASES.get(value, value)


def _aliases(candidate: Candidate) -> set[str]:
    return {_identity(value) for value in (candidate.title, PurePosixPath(candidate.name).stem) if value}


def _coverage(candidate: Candidate) -> int:
    text = "\n".join(line for line in candidate.text.splitlines() if not line.lstrip().startswith(("#", "//")))
    if re.search(r"\[Script\]|\[Body Rewrite\]|\b(?:script-(?:request|response)|(?:request|response)[.-](?:body|json))", text):
        return 2
    if re.search(r"\[(?:Rewrite|URL Rewrite|Map Local|Header Rewrite)\]| url ", text):
        return 1
    return 0


def discover() -> list[Candidate]:
    payload = json.loads(fetch_text(CATALOG_URL))
    candidates = []
    for item in payload.get("lists", []):
        url = plugin_url(item["url"])
        path = PurePosixPath(urlsplit(url).path)
        if path.suffix.lower() != ".lpx":
            raise ValueError(f"catalog entry is not an LPX plugin: {url}")
        adblock = "去广告" in item.get("tag", []) and "重定向" not in item["name"]
        candidates.append(Candidate(path.stem + ".sgmodule", url, "Kelee", "loon", item["name"], adblock))
    if not candidates:
        raise ValueError("Kelee catalog contains no plugins")
    for upstream, tree_url in TREES.items():
        tree = json.loads(fetch_text(tree_url))
        if tree.get("truncated") or not tree.get("tree"):
            raise ValueError(f"incomplete GitHub tree: {tree_url}")
        for entry in tree["tree"]:
            path = entry["path"]
            if upstream == "fmz200":
                if not (path.startswith("Surge/module/split/") and path.endswith(".sgmodule")):
                    continue
                url = "https://raw.githubusercontent.com/fmz200/wool_scripts/main/" + path
                kind = "surge"
            else:
                if not (path.startswith("AdBlock/") and path.endswith(".conf")) or path == "AdBlock/YoutubeAds.conf":
                    continue
                url = "https://raw.githubusercontent.com/ddgksf2013/Rewrite/master/" + path
                kind = "quantumult"
            candidates.append(Candidate(PurePosixPath(path).stem + ".sgmodule", url, upstream, kind))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        texts = executor.map(fetch_text, (candidate.url for candidate in candidates))
        for candidate, text in zip(candidates, texts):
            candidate.text = text
            if not candidate.title:
                pattern = r"^#!name=(.+)" if candidate.format == "surge" else r"^//\s*@ScriptName\s+(.+)"
                match = re.search(pattern, text, re.MULTILINE)
                if not match:
                    raise ValueError(f"missing plugin title: {candidate.url}")
                candidate.title = match.group(1).strip()
    return candidates


def select(candidates: list[Candidate]) -> tuple[list[Candidate], dict, list[dict]]:
    groups: list[list[Candidate]] = []
    aliases: list[set[str]] = []
    excluded = []
    for candidate in candidates:
        reason = ""
        if "已失效" in candidate.title:
            reason = "upstream explicitly marks this plugin as retired"
        elif candidate.upstream == "fmz200" and _coverage(candidate) == 0:
            reason = "domain-only fragment; omitted from the individual app catalog"
        elif candidate.upstream == "fmz200" and PurePosixPath(candidate.name).stem in {"AdAlliance", "WeChatUnlockLinkRestrict", "Alibaba"}:
            reason = "shared ad-network or non-ad utility; existing Kelee modules cover this role"
        elif candidate.upstream == "ddgksf2013" and candidate.name == "WeiboAds.sgmodule":
            reason = "combines Weibo editions; retain separately selectable app modules"
        if reason:
            excluded.append({"url": candidate.url, "reason": reason})
            continue
        names = _aliases(candidate) if candidate.adblock else {"utility:" + candidate.name}
        matches = [index for index, known in enumerate(aliases) if names & known]
        if matches:
            index = matches[0]
            groups[index].append(candidate)
            aliases[index].update(names)
            for other in reversed(matches[1:]):
                groups[index].extend(groups.pop(other))
                aliases[index].update(aliases.pop(other))
        else:
            groups.append([candidate])
            aliases.append(names)
    selected = []
    decisions = {}
    legacy_names = {candidate.name for candidate in candidates if candidate.upstream == "Kelee"}
    for group in groups:
        # Keep published filenames stable, even when another source wins.
        legacy = next((candidate for candidate in group if candidate.upstream == "Kelee"), None)
        ranked = sorted(group, key=lambda candidate: (-_coverage(candidate), SOURCE_ORDER[candidate.upstream], candidate.url))
        winner = ranked[0]
        if legacy and legacy.name == "YouTube_remove_ads.sgmodule":
            winner = legacy  # generate.py preserves the ddgks-recommended Maasea module.
        name = legacy.name if legacy else winner.name
        if not legacy and name in legacy_names:
            name = PurePosixPath(name).stem + "_remove_ads.sgmodule"
        if name in decisions:
            raise ValueError(f"unrelated applications share filename: {name}")
        decisions[name] = {
            "upstream": winner.upstream,
            "format": winner.format,
            "adblock": winner.adblock,
            "app_aliases": sorted(set().union(*(_aliases(candidate) for candidate in group))),
            "coverage": ("domains", "rewrites", "body-or-script")[_coverage(winner)],
            "source_sha256": hashlib.sha256(winner.text.encode()).hexdigest(),
            "reason": "prefer body/script cleanup, then rewrites, then domain rules; ties: Kelee, fmz200, ddgksf2013",
            "alternatives": [{"upstream": candidate.upstream, "url": candidate.url} for candidate in ranked if candidate is not winner],
        }
        selected.append(replace(winner, name=name))
        for candidate in group:
            if candidate.upstream == "Kelee" and candidate.name != name:
                decisions[candidate.name] = {**decisions[name], "alias_of": name}
                selected.append(replace(winner, name=candidate.name))
    return selected, decisions, excluded


def surge_module(candidate: Candidate) -> str:
    metadata = {}
    body = []
    section = ""
    for number, line in enumerate(candidate.text.splitlines(), 1):
        if line.startswith("#!") and "=" in line:
            key, value = line[2:].split("=", 1)
            metadata[key] = value
        else:
            if line.startswith("[") and line.endswith("]"):
                section = line
            if section == "[Script]" and line.strip() and not line.lstrip().startswith(("#", ";", "[")):
                _, separator, value = line.partition("=")
                if not separator:
                    raise ValueError(f"invalid Surge script: {line}")
                line = f"{PurePosixPath(candidate.name).stem}-{number} ={value}"
            if section == "[Body Rewrite]" and re.match(r"http-(?:request|response)\s", line):
                direction, pattern, replacements = line.split(maxsplit=2)
                fields = shlex.split(replacements, posix=False)
                if len(fields) % 2:
                    raise ValueError(f"invalid Surge body replacements: {line}")
                fields = [field if field.startswith(("'", '"')) and field[-1] == field[0]
                          else quote_body_field(field) for field in fields]
                line = f"{direction} {pattern} {' '.join(fields)}"
            match = re.fullmatch(r"(http-(?:request|response)-jq .* )'(.*)'", line)
            if match:
                line = match.group(1) + "'" + _surge_safe_jq(match.group(2)) + "'"
            body.append(line)
    header = {
        "name": metadata.pop("name"),
        "desc": metadata.pop("desc", "") or f"{candidate.title}广告拦截。",
        "category": "去广告",
    }
    metadata.pop("category", None)
    if "[Map Local]" in body or "[Body Rewrite]" in body:
        metadata["requirement"] = "CORE_VERSION>=20"
    return "\n".join([
        *[f"#!{key}={value}" for key, value in (header | metadata).items()],
        "", f"# Source: {candidate.url}",
        "# Modified by SelfSurge: normalized metadata, jq syntax, and resource URLs.",
        "# License: GPL-3.0; see resources/licenses/fmz200-wool_scripts-LICENSE.",
        *body,
    ]).rstrip() + "\n"
