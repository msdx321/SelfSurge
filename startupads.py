"""Convert StartUpAds into a supplement to the generated app modules."""

from collections import Counter
from fnmatch import fnmatchcase
import re

from selfsurge import _convert_rewrite


STARTUP_MODULE_NAME = "StartUpAds.sgmodule"
STARTUP_MODULE_URL = "https://ddgksf2013.top/rewrite/StartUpAds.conf"


def _authority(pattern: str) -> str:
    pattern = pattern.replace(r"\/", "/").removeprefix("^")
    match = re.match(r"https?\??://([^/]+)", pattern)
    if not match:
        return ""
    try:
        re.compile(match[1])
    except re.error:
        # A group spanning the host and path cannot be treated as a host regex.
        return ""
    return match[1]


def _literal_host(authority: str) -> str:
    host = authority.replace(r"\.", ".")
    return host.lower() if re.fullmatch(r"[\w.-]+(?::\d+)?", host) else ""


def startup_module(source: str, modules: list[str]) -> str:
    if "// @ScriptName" not in source or "hostname =" not in source:
        raise ValueError("StartUpAds response is not a Quantumult X rewrite file")

    patterns = set()
    authorities = set()
    owned_hosts = set()
    blocked_hosts = set()
    for module in modules:
        ad_module = "remove_ads" in module or "Remove_ads_by_keli" in module
        section = ""
        for line in module.splitlines():
            if line.startswith("["):
                section = line
            if not line or line.startswith(("#", "[")):
                continue
            pattern = ""
            if section in {"[URL Rewrite]", "[Map Local]"}:
                pattern = line.split()[0]
            elif section in {"[Body Rewrite]", "[Header Rewrite]"}:
                pattern = line.split()[1]
            elif section == "[Script]":
                match = re.search(r"\bpattern=(.*?),\s*\w[\w-]*=", line)
                if match:
                    pattern = match[1]
            elif section == "[Rule]":
                fields = [field.strip().strip('"') for field in line.split(",")]
                if len(fields) >= 3 and fields[2].startswith("REJECT"):
                    if fields[0] == "DOMAIN":
                        blocked_hosts.add(fields[1].lower())
                    elif fields[0] == "DOMAIN-SUFFIX":
                        blocked_hosts.update((fields[1].lower(), "*." + fields[1].lower()))
                    elif fields[0] == "URL-REGEX":
                        pattern = fields[1]
            elif section == "[MITM]" and ad_module and line.startswith("hostname"):
                owned_hosts.update(
                    host.strip().lower() for host in line.split("=", 1)[1].replace("%APPEND%", "").split(",")
                    if host.strip() and not host.strip().startswith("-")
                )
            if pattern:
                patterns.add(pattern.replace(r"\/", "/"))
                authority = _authority(pattern)
                # Only ad modules claim app hosts; utility scripts do not.
                if authority and ad_module:
                    authorities.add(authority)

    sections = {}
    retained_authorities = set()
    skipped = Counter()
    hosts = []
    label = ""
    seen = set()
    date = re.search(r"// @UpdateTime\s+(\S+)", source)
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if line.startswith("# >"):
            label = line[3:].strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        if line.startswith("hostname"):
            hosts.extend(host.strip() for host in line.split("=", 1)[1].split(","))
            continue
        if line.startswith(("host,", "host-suffix,")):
            kind, host, policy = [part.strip() for part in line.split(",")]
            if policy != "reject":
                skipped["non-ad routing override"] += 1
                continue
            candidates = (host, "*." + host) if kind == "host-suffix" else (host,)
            if all(any(fnmatchcase(h, old) for old in blocked_hosts) for h in candidates):
                skipped["existing domain rule"] += 1
                continue
            rule = f"{'DOMAIN-SUFFIX' if kind == 'host-suffix' else 'DOMAIN'}, {host}, REJECT"
            sections.setdefault("[Rule]", []).append(rule)
            blocked_hosts.update(candidates)
            continue
        pattern, separator, operation = line.partition(" url ")
        if not separator:
            raise ValueError(f"StartUpAds:{number}: unsupported rule: {line}")
        authority = _authority(pattern)
        host = _literal_host(authority).split(":", 1)[0]
        if label.lower() == "version" or host in {"testflight.apple.com", "gw.xiaocantech.com"}:
            skipped["version marker or non-ad tweak"] += 1
            continue
        normalized = pattern.replace(r"\/", "/")
        if normalized in patterns or authority in authorities or (
            host and any(fnmatchcase(host, old) for old in blocked_hosts | owned_hosts)
        ) or (
            authority and any(
                re.fullmatch(authority, old)
                for old in owned_hosts | blocked_hosts if "*" not in old
            )
        ):
            skipped["existing module coverage"] += 1
            continue
        if normalized in seen:
            skipped["duplicate upstream pattern"] += 1
            continue
        seen.add(normalized)
        action, _, value = operation.partition(" ")
        if action.startswith("script-"):
            if action not in {"script-response-body", "script-response-header", "script-analyze-echo-response"}:
                raise ValueError(f"StartUpAds:{number}: unsupported action: {action}")
            direction = "request" if action == "script-analyze-echo-response" else "response"
            body = ", requires-body=true, max-size=0" if action == "script-response-body" else ""
            converted = [("[Script]", f"startup-{number} = type=http-{direction}, pattern={pattern}, script-path={value}{body}, timeout=60")]
        elif action in {"response-body", "request-body"}:
            search, separator, replacement = value.partition(f" {action} ")
            if not separator:
                raise ValueError(f"StartUpAds:{number}: invalid body replacement")
            converted = _convert_rewrite(f"{pattern} {action}-replace-regex {search} {replacement}", {})
        elif action == "jsonjq-response-body":
            converted = _convert_rewrite(f"{pattern} response-body-json-jq {value}", {})
        elif action == "echo-response":
            content_type, separator, url = value.partition(" echo-response ")
            if not separator or not url.startswith("https://"):
                raise ValueError(f"StartUpAds:{number}: invalid echo response")
            converted = [("[Map Local]", f'{pattern} data="{url}" header="Content-Type:{content_type}" status-code=200')]
        else:
            converted = _convert_rewrite(f"{pattern} {operation}", {})
        for section, rule in converted:
            sections.setdefault(section, []).extend((f"# {label}", rule))
        if authority and not pattern.startswith("^http:"):
            retained_authorities.add(authority)

    # Keep wildcard declarations conservatively; prune unused concrete hosts.
    hosts = list(dict.fromkeys(
        host for host in hosts
        if not any(fnmatchcase(host, old) for old in owned_hosts | blocked_hosts)
        and ("*" in host or any(
            re.fullmatch(authority, host) or re.fullmatch(authority, host + ":443")
            or re.fullmatch(re.sub(r"\\?:\d+$", "", authority), host)
            for authority in retained_authorities
        ))
    ))
    if not seen:
        raise ValueError("StartUpAds contains no supplemental rewrites")
    header = [
        "#!name=墨鱼去开屏 2.0 · 补充规则",
        "#!desc=补充现有去广告模块未覆盖的主机；请配合现有模块使用。",
        "#!category=去广告",
        "#!author=ddgksf2013",
        f"#!date={date[1] if date else ''}",
        "#!requirement=CORE_VERSION>=20",
        "",
        f"# Source: {STARTUP_MODULE_URL}",
        "# Adapted by SelfSurge from Quantumult X; processed after all other modules.",
        "# Existing ad modules own their URL authorities; their rules take precedence.",
        "# Broad rules matching an existing app host are excluded to avoid conflicts.",
        "# Upstream attribution:",
        *["# " + line.removeprefix("// ") for line in source.splitlines() if line.startswith("// @")],
        "",
        *[f"# Removed {count}: {reason}" for reason, count in sorted(skipped.items())],
        f"# Retained {len(seen)} supplemental rewrites.",
    ]
    for section, rules in sections.items():
        header.extend(("", section, *rules))
    if hosts:
        header.extend(("", "[MITM]", "hostname = %APPEND% " + ", ".join(hosts)))
    return "\n".join(header) + "\n"
