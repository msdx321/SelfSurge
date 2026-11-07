"""Full-coverage general adblock with exclusions for enabled app modules."""

import re
from collections import defaultdict
from pathlib import PurePosixPath

from quantumult import convert_qx
from upstreams import _identity


STARTUP_MODULE_NAME = "StartUpAds.sgmodule"
STARTUP_MODULE_URL = "https://ddgksf2013.top/rewrite/StartUpAds.conf"

# StartUpAds uses pinyin labels where the app catalogs use English filenames.
LABEL_ALIASES = {
    "jingdong": "jd", "baiduwangpan": "baidunetdisk",
    "huazhu": "huazhuclub", "qichezhijia": "autohome",
    "jiaxiaoediantong": "jiaxiaoyidiantong", "kuake": "quark",
    "qidianyuedu": "qidian", "qidiandushu": "qidian",
    "shenmezhidemai": "smzdm", "shunfeng": "sfexpress",
    "wangyiyouxiang": "mailmaster", "xiangrikui": "oraysunlogin",
    "xiaomishangcheng": "xiaomimall", "xiaoaiyinxiang": "小爱音箱",
    "yingshiyun": "ezviz", "zhongguoliantong": "chinaunicom",
    "zhangyue": "ireader",
}


def startup_module(source: str) -> str:
    module = convert_qx(source, STARTUP_MODULE_URL, general=True)
    lines = module.splitlines()
    lines[0] = "#!name=通用去广告兜底 · 墨鱼去开屏"
    lines[1] = (
        "#!desc=默认保留完整通用规则，可与单独应用模块搭配。"
        "请将已启用应用的排除参数填入 exclude_apps，通用模块继续覆盖其余规则。"
    )
    return "\n".join(lines) + "\n"


def _pattern(section: str, line: str) -> str:
    if section in {"[URL Rewrite]", "[Map Local]"}:
        return line.split()[0]
    if section in {"[Body Rewrite]", "[Header Rewrite]"}:
        return line.split()[1]
    if section == "[Script]":
        match = re.search(r"\bpattern=(.*?)(?=,\s*[\w-]+=)", line)
        if match:
            return match[1].strip('"')
    return ""


def _authority(pattern: str) -> str:
    pattern = re.sub(r"^\(\?[a-z-]+\)", "", pattern.replace(r"\/", "/"))
    match = re.match(r"\^?https?\??://([^/]+)", pattern)
    return match[1].replace(r"\.", ".").lower().split(":", 1)[0] if match else ""


def configure_fallback(module: str, app_modules: dict[str, str], decisions: dict) -> tuple[str, dict[str, int]]:
    """Gate app-owned rules; the default none preserves full coverage."""
    aliases = defaultdict(set)
    patterns = defaultdict(set)
    authorities = defaultdict(set)
    domains = defaultdict(set)
    keys = {}
    for name, source in app_modules.items():
        decision = decisions[name]
        if not decision.get("adblock") or "alias_of" in decision:
            continue
        key = PurePosixPath(name).stem
        keys[name] = key
        for alias in decision["app_aliases"]:
            aliases[alias].add(name)
        section = ""
        for line in source.splitlines():
            if line.startswith("["):
                section = line
            if not line or line.startswith(("#", "[")):
                continue
            pattern = _pattern(section, line)
            if pattern:
                patterns[pattern.replace(r"\/", "/")].add(name)
                authority = _authority(pattern)
                if authority:
                    authorities[authority].add(name)
            elif section == "[Rule]":
                fields = [field.strip() for field in line.split(",")]
                if len(fields) >= 3 and fields[0] in {"DOMAIN", "DOMAIN-SUFFIX"} and fields[2].startswith("REJECT"):
                    domains[(fields[0], fields[1])].add(name)

    counts = defaultdict(int)
    output = []
    section = label = ""
    for line in module.splitlines():
        if line.startswith("#!requirement="):
            line = "#!requirement=CORE_VERSION>=22"
            output.extend([
                "#!arguments=exclude_apps:none",
                "#!arguments-desc=在目录页勾选已启用的应用，复制排除参数到此处。多个模块标识以 | 分隔；none 表示不排除，保留全部规则。此参数不会自动读取 Surge 的模块启用状态。",
            ])
        if line.startswith("["):
            section = line
        if line.startswith("# ") and not line.startswith("# QX action:"):
            label = line[2:]
        if not line or line.startswith(("#", "[")) or section == "[MITM]":
            output.append(line)
            continue
        identity = _identity(re.sub(r"\[[^]]*\]", "", label))
        label_owners = aliases.get(LABEL_ALIASES.get(identity, identity), set())
        pattern = _pattern(section, line)
        variants = [(line, None)]
        if pattern:
            # Split literal host alternatives when they belong to different
            # apps, e.g. UC / Quark. Excluding Quark must keep UC's rule active.
            group = re.search(r"\(([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)+)\)", _authority(pattern))
            if group:
                branches = []
                for index, host in enumerate(group[1].split("|")):
                    branch_pattern = pattern.replace(group[0], host, 1)
                    owners = authorities.get(_authority(branch_pattern), set())
                    owners = owners if len(owners) == 1 else set()
                    branch = line.replace(pattern, branch_pattern, 1)
                    if section == "[Script]":
                        name, value = branch.split("=", 1)
                        branch = f"{name.strip()}-host{index} ={value}"
                    branches.append((branch, owners))
                if any(owners for _, owners in branches) and len({frozenset(owners) for _, owners in branches}) > 1:
                    variants = branches
        for variant, forced_owners in variants:
            owners = set() if forced_owners is None else set(forced_owners)
            if forced_owners is None:
                owners.update(patterns.get(pattern.replace(r"\/", "/"), ()))
                host_owners = authorities.get(_authority(pattern), set())
                if not owners and len(host_owners) == 1:
                    owners.update(host_owners)
                if section == "[Rule]":
                    fields = [field.strip() for field in variant.split(",")]
                    owners.update(domains.get(tuple(fields[:2]), ()))
                if not owners:
                    owners.update(label_owners)
            if owners:
                conditions = []
                for name in sorted(owners):
                    counts[name] += 1
                    accepted = [keys[name], *[
                        PurePosixPath(alias).stem for alias, decision in decisions.items()
                        if decision.get("alias_of") == name
                    ]]
                    conditions.extend(f"NOT ('|{{{{{{exclude_apps}}}}}}|' CONTAINS '|{key}|')" for key in accepted)
                variant = '#!REQUIREMENT "' + " AND ".join(conditions) + '" ' + variant
            output.append(variant)
    return "\n".join(output) + "\n", dict(counts)
