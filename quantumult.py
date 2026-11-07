"""Convert Quantumult X adblock rewrites, preserving upstream attribution."""

import hashlib
import re

from selfsurge import _convert_rewrite


def quote_body_field(value: str) -> str:
    quote = "'" if "'" not in value else '"'
    if quote in value:
        raise ValueError(f"body field contains both quote delimiters: {value}")
    return quote + value + quote


def convert_qx(source: str, source_url: str, *, general: bool = False) -> str:
    metadata = {}
    for key, value in re.findall(r"^//\s*@(\w+)\s+([^\n]+)", source, re.MULTILINE):
        metadata.setdefault(key, value)
    if not metadata.get("ScriptName") or "hostname" not in source:
        raise ValueError(f"not a Quantumult X rewrite: {source_url}")
    sections: dict[str, list[str]] = {}
    seen = set()
    hosts = []
    label = ""
    skipped = 0
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if line.startswith("# >"):
            label = line[3:].strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        if re.match(r"hostname\s*=", line):
            hosts.extend(host.strip() for host in line.split("=", 1)[1].split(","))
            continue
        if re.match(r"(?:host|host-suffix|host-keyword|ip-cidr),", line, re.I):
            kind, host, policy = [part.strip() for part in line.split(",")]
            if policy.lower() not in {"reject", "direct"}:
                raise ValueError(f"{source_url}:{number}: unsupported policy: {policy}")
            if general and policy.lower() != "reject":
                skipped += 1
                continue
            kind = {"host": "DOMAIN", "host-suffix": "DOMAIN-SUFFIX",
                    "host-keyword": "DOMAIN-KEYWORD", "ip-cidr": "IP-CIDR"}[kind.lower()]
            sections.setdefault("[Rule]", []).append(f"{kind}, {host}, {policy.upper()}")
            continue
        pattern, separator, operation = line.partition(" url ")
        if not separator:
            raise ValueError(f"{source_url}:{number}: unsupported rule: {line}")
        if general and (label.lower() == "version" or any(
            host in pattern.replace(r"\.", ".")
            for host in ("testflight.apple.com", "gw.xiaocantech.com")
        )):
            skipped += 1
            continue
        # Request and response actions on the same URL are complementary.
        identity = (pattern.replace(r"\/", "/"), operation)
        if identity in seen:
            continue
        seen.add(identity)
        action, _, value = operation.partition(" ")
        notes = []
        if action.startswith("script-"):
            if action not in {
                "script-request-header", "script-request-body",
                "script-response-header", "script-response-body",
                "script-echo-response", "script-analyze-echo-response",
            }:
                raise ValueError(f"{source_url}:{number}: unsupported action: {action}")
            direction = "response" if action.startswith("script-response") else "request"
            body = ", requires-body=true, max-size=-1" if action.endswith("-body") or action == "script-analyze-echo-response" else ""
            pattern = f'"{pattern}"' if "," in pattern else pattern
            notes = [f"# QX action: {action}"]
            identifier = hashlib.sha256(source_url.encode()).hexdigest()[:8]
            converted = [("[Script]", f"qx-{identifier}-{number} = type=http-{direction}, pattern={pattern}, script-path={value}{body}, timeout=60")]
        elif action in {"response-body", "request-body"}:
            search, separator, replacement = value.partition(f" {action} ")
            if not separator:
                raise ValueError(f"{source_url}:{number}: invalid body replacement")
            fields = [quote_body_field(field) for field in (search, replacement)]
            converted = _convert_rewrite(f"{pattern} {action}-replace-regex {' '.join(fields)}", {})
        elif action == "jsonjq-response-body":
            converted = _convert_rewrite(f"{pattern} response-body-json-jq {value}", {})
        elif action == "echo-response":
            content_type, separator, url = value.partition(" echo-response ")
            if not separator or not url.startswith("https://"):
                raise ValueError(f"{source_url}:{number}: invalid echo response")
            converted = [("[Map Local]", f'{pattern} data="{url}" header="Content-Type:{content_type}" status-code=200')]
        else:
            converted = _convert_rewrite(f"{pattern} {operation}", {})
        for section, rule in converted:
            sections.setdefault(section, []).extend([f"# {label}", *notes, rule])
    if not seen:
        raise ValueError(f"no rewrites in {source_url}")
    output = [
        f'#!name={metadata["ScriptName"]}',
        "#!desc=" + metadata.get("Function", "应用广告拦截与界面净化。"),
        "#!category=去广告",
        "#!author=" + metadata.get("Author", "ddgksf2013"),
        "#!date=" + metadata.get("UpdateTime", ""),
        "#!requirement=CORE_VERSION>=20",
        "",
        f"# Source: {source_url}",
        "# Adapted from Quantumult X by SelfSurge; upstream attribution:",
        *["# " + line.removeprefix("// ") for line in source.splitlines() if line.startswith("// @")],
    ]
    if skipped:
        output.append(f"# Excluded {skipped} routing overrides and non-ad tweaks.")
    for section, rules in sections.items():
        output.extend(["", section, *rules])
    if hosts:
        if general:
            hosts = [host for host in hosts if host not in {"testflight.apple.com", "gw.xiaocantech.com"}]
        output.extend(["", "[MITM]", "hostname = %APPEND% " + ", ".join(dict.fromkeys(hosts))])
    return "\n".join(output) + "\n"


def adapt_script(source: str, action: str) -> str:
    """Translate QX status strings and echo results to Surge's result API."""
    echo = "true" if action in {"script-echo-response", "script-analyze-echo-response"} else "false"
    return (
        "// Adapted by SelfSurge: normalize Quantumult X $done results for Surge.\n"
        "(function ($done) {\n" + source + "\n})((result) => {\n"
        "  if (result && typeof result === 'object') {\n"
        f"    if ({echo} && !result.response && Object.keys(result).length) {{\n"
        "      result = {response: result};\n"
        "    }\n"
        "    const response = result.response || result;\n"
        "    if (typeof response.status === 'string') {\n"
        "      const code = response.status.match(/^(?:HTTP\\/\\S+\\s+)?(\\d{3})(?:\\s|$)/);\n"
        "      if (code) response.status = Number(code[1]);\n"
        "    }\n"
        "  }\n"
        "  $done(result);\n"
        "});\n"
    )
