import argparse
import base64
import json
import re
import sys
from functools import cache
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen


LOON_USER_AGENT = "Loon/1022 CFNetwork/1498.700.2 Darwin/23.6.0"
PUBLISHED_RESOURCE_PREFIX = (
    "https://raw.githubusercontent.com/msdx321/SelfSurge/main/resources/"
)
PUBLISHED_SCRIPT_PREFIX = (
    "https://raw.githubusercontent.com/msdx321/SelfSurge/main/scripts/"
)
CC_LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"

_PATH_PART = re.compile(
    r"\.?([^\.\[\]]+)|\[(['\"])(.*?)\2\]|\[(\d+)\]"
)
_RESOURCE_URL = re.compile(
    r"https://kelee\.one/Resource/[^\s\"'\\),]+"
)


def plugin_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "loon":
        plugins = parse_qs(parsed.query).get("plugin")
        if parsed.netloc != "import" or not plugins:
            raise ValueError("invalid Loon import URL")
        value = plugins[0]
        parsed = urlsplit(value)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("plugin URL must use HTTP or HTTPS")
    return value


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": LOON_USER_AGENT})
    error = None
    for _ in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as caught:
            if caught.code < 500:
                raise
            error = caught
        except OSError as caught:
            error = caught
    raise error or OSError(f"failed to fetch {url}")


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8-sig")


def fetch_lpx(url: str) -> str:
    source = fetch_text(plugin_url(url))
    if not any(line.startswith("#!name=") for line in source.splitlines()):
        raise ValueError("response is not an LPX plugin")
    return source


def resource_urls(source: str) -> set[str]:
    return set(_RESOURCE_URL.findall(source))


def published_resource_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "kelee.one"
        or not parsed.path.startswith("/Resource/")
    ):
        return url

    relative = parsed.path.removeprefix("/Resource/")
    if relative.startswith("JavaScript/"):
        return PUBLISHED_SCRIPT_PREFIX + quote(
            relative.removeprefix("JavaScript/"), safe="/"
        )
    return PUBLISHED_RESOURCE_PREFIX + quote(relative, safe="/")


def _split_parameters(value: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    quote_char = None
    escaped = False

    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote_char:
            if char == quote_char:
                quote_char = None
        elif char in {'"', "'"}:
            quote_char = char
        elif char in "[({":
            depth += 1
        elif char in "])}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1

    if quote_char or depth:
        raise ValueError(f"unbalanced parameters: {value}")
    parts.append(value[start:].strip())
    return parts


def _arguments(
    source: str,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    section = None
    names = {}
    kinds = {}
    defaults = []
    notes = []

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section != "[Argument]" or not stripped or stripped.startswith("#"):
            continue

        key, separator, value = stripped.partition("=")
        fields = _split_parameters(value)
        if not separator or len(fields) < 2:
            raise ValueError(f"invalid argument: {stripped}")

        key = key.strip()
        kind = fields[0]
        surge_key = re.sub(r"[^A-Za-z0-9_]", "_", key)
        if not surge_key or surge_key in names.values():
            raise ValueError(f"invalid or duplicate Surge argument: {key}")

        values = []
        tag = ""
        description = ""
        for field in fields[1:]:
            if field.startswith("tag="):
                tag = field.removeprefix("tag=")
            elif field.startswith("desc="):
                description = field.removeprefix("desc=")
            else:
                values.append(field.strip().strip('"'))
        if not values:
            raise ValueError(f"argument has no default: {stripped}")

        names[key] = surge_key
        kinds[key] = kind
        defaults.append(f"{surge_key}:{values[0]}")
        details = [f"默认 {values[0]}"]
        if kind in {"select", "switch"}:
            details.append("可选 " + " | ".join(values))
        if tag:
            details.append(tag)
        if description:
            details.append(description)
        notes.append(f"# Surge 参数 {surge_key}：" + "；".join(details))

    return names, kinds, defaults, notes


def _replace_placeholders(value: str, arguments: dict[str, str]) -> str:
    for loon_name, surge_name in arguments.items():
        value = value.replace(
            "{" + loon_name + "}", "{{{" + surge_name + "}}}"
        )
    return value


@cache
def _script_argument_style(url: str) -> str:
    try:
        script = fetch_text(url)
    except (OSError, UnicodeError):
        return "object"
    if re.search(r"JSON\.parse\(\s*\$argument\s*\)", script):
        return "json"
    if re.search(
        r"\$argument(?:\?\.)?(?:\.split|\[['\"]split['\"]\])"
        r"\(\s*['\"]&['\"]",
        script,
    ):
        return "query"
    return "object"


def _json_path(value: str) -> list[str | int]:
    path = []
    for match in _PATH_PART.finditer(value.strip()):
        if match[1] is not None:
            path.append(match[1])
        elif match[3] is not None:
            path.append(match[3])
        else:
            path.append(int(match[4]))
    if not path:
        raise ValueError(f"invalid JSON path: {value}")
    return path


def _loon_value(value: str):
    value = value.replace(r"\x20", " ")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip('"\'')


def _normalize_jq(value: str) -> str:
    output = []
    conditions = []
    in_string = False
    escaped = False
    index = 0

    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if not (char.isalpha() or char == "_"):
            output.append(char)
            index += 1
            continue

        end = index + 1
        while end < len(value) and (
            value[end].isalnum() or value[end] == "_"
        ):
            end += 1
        word = value[index:end]
        keyword = False
        if word == "if":
            conditions.append(False)
            keyword = True
        elif word == "else" and conditions:
            conditions[-1] = True
            keyword = True
        elif word == "end" and conditions:
            if not conditions.pop():
                output.append(" else . ")
            keyword = True
        elif word in {"then", "and", "or"} and conditions:
            keyword = True

        if keyword and output and not output[-1].isspace():
            output.append(" ")
        output.append(word)
        if keyword and end < len(value) and not value[end].isspace():
            output.append(" ")
        index = end
    return "".join(output).strip()


def _surge_safe_jq(value: str) -> str:
    output = []
    in_string = False
    escaped = False
    index = 0

    while index < len(value):
        char = value[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "#":
            index = value.find("\n", index)
            if index == -1:
                break
            if output and not output[-1].isspace():
                output.append(" ")
            index += 1
            continue
        if char.isspace():
            if output and not output[-1].isspace():
                output.append(" ")
            index += 1
            continue
        if char == ";" or value.startswith("//", index):
            while output and output[-1].isspace():
                output.pop()
        output.append(char)
        index += 1
    return "".join(output).strip()


def _jq(value: str) -> str:
    value = value.strip()
    if value.startswith('jq-path="') and value.endswith('"'):
        value = fetch_text(value[9:-1])
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    if not value:
        value = "."
    if "'" in value:
        raise ValueError("JQ expression contains an unsupported single quote")
    value = _surge_safe_jq(value)
    return f"'{_surge_safe_jq(_normalize_jq(value))}'"


def _mock_response(pattern: str, value: str) -> str:
    data_type = re.search(r"\bdata-type=([^\s]+)", value)
    status = re.search(r"\bstatus-code=(\d+)", value)
    data_path = re.search(r'\bdata-path="([^"]+)"', value)
    base64_data = re.search(r"\bmock-data-is-base64=true\b", value)
    data_match = re.search(
        r'\bdata="(.*)"(?:\s+status-code=\d+|\s+mock-data-is-base64=true)?$',
        value,
    )
    kind = data_type.group(1) if data_type else "text"
    status_code = status.group(1) if status else "200"

    if data_path:
        return (
            f'{pattern} data-type=file data="{data_path.group(1)}" '
            f"status-code={status_code}"
        )

    data = data_match.group(1) if data_match else ""
    if base64_data:
        return (
            f'{pattern} data-type=base64 data="{data}" '
            f"status-code={status_code}"
        )
    if not data:
        return f'{pattern} data-type=text data="" status-code={status_code}'

    encoded = base64.b64encode(data.encode()).decode()
    content_type = "application/json" if kind == "json" else "text/plain"
    return (
        f'{pattern} data-type=base64 data="{encoded}" '
        f'header="Content-Type:{content_type}" status-code={status_code}'
    )


def _conditional_rewrite(
    line: str, arguments: dict[str, str]
) -> tuple[str, str] | None:
    match = re.fullmatch(
        r'request if \$\{url\} ~= /(.+)/ as item then redirect\('
        r'(302|307), "\$\{app\}://(resolve\?domain|join\?invite)='
        r'\$\{item\.1\}"\)',
        line,
    )
    if not match:
        return None
    app = arguments.get("app", "app")
    return (
        "[URL Rewrite]",
        f"{match.group(1)} "
        + "{{{"
        + app
        + "}}}"
        + f"://{match.group(3)}=$1 {match.group(2)}",
    )


def _convert_rewrite(
    line: str, arguments: dict[str, str]
) -> list[tuple[str, str]]:
    conditional = _conditional_rewrite(line, arguments)
    if conditional:
        return [conditional]

    parts = line.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError(f"invalid rewrite rule: {line}")

    if parts[0] in {"http-request", "http-response"}:
        if len(parts) != 3:
            raise ValueError(f"invalid rewrite rule: {line}")
        pattern = parts[1]
        action, _, value = parts[2].partition(" ")
    else:
        pattern, action = parts[:2]
        value = parts[2] if len(parts) == 3 else ""

    reject_actions = {
        "reject",
        "reject-dict",
        "reject-array",
        "reject-200",
        "reject-img",
    }
    if action in reject_actions and value == action:
        value = ""

    if action == "reject" and not value:
        return [("[URL Rewrite]", f"{pattern} _ reject")]
    if action in {"reject-dict", "reject-array"} and not value:
        data = "{}" if action == "reject-dict" else "[]"
        return [
            (
                "[Map Local]",
                f'{pattern} data-type=text data="{data}" '
                'header="Content-Type:application/json" status-code=200',
            )
        ]
    if action == "reject-200" and not value:
        return [
            ("[Map Local]", f'{pattern} data-type=text data="" status-code=200')
        ]
    if action == "reject-img" and not value:
        return [("[Map Local]", f"{pattern} data-type=tiny-gif status-code=200")]
    if action in {"302", "307", "header"} and value:
        return [("[URL Rewrite]", f"{pattern} {value} {action}")]
    if action == "mock-response-body":
        return [("[Map Local]", _mock_response(pattern, value))]

    json_action = re.fullmatch(
        r"(request|response)-body-json-(jq|add|del|replace)", action
    )
    if json_action:
        http_type, operation = json_action.groups()
        surge_type = f"http-{http_type}-jq"
        if operation == "jq":
            expression = _jq(value)
            return [("[Body Rewrite]", f"{surge_type} {pattern} {expression}")]
        if operation == "del" and re.fullmatch(
            r"'del(?:paths)?\s*\(.*\)'", value
        ):
            return [
                ("[Body Rewrite]", f"{surge_type} {pattern} {_jq(value)}")
            ]

        words = value.split()
        if operation == "del":
            paths = [_json_path(word.replace(r"\x20", " ")) for word in words]
            paths_json = json.dumps(
                paths, ensure_ascii=False, separators=(",", ":")
            )
            expression = (
                f"reduce {paths_json}[] as $path (. ;"
                ". as $before | try delpaths([$path]) catch $before)"
            )
        else:
            if len(words) % 2:
                raise ValueError(f"invalid JSON rewrite pairs: {line}")
            items = []
            for key, raw_value in zip(words[::2], words[1::2]):
                path = _json_path(key.replace(r"\x20", " "))
                converted_value = _loon_value(raw_value)
                if operation == "add":
                    items.append([path, converted_value])
                    continue
                items.append([path, path[:-1], path[-1], converted_value])
            items_json = json.dumps(
                items, ensure_ascii=False, separators=(",", ":")
            )
            if operation == "add":
                expression = (
                    f"reduce {items_json}[] as $item (. ;"
                    ". as $before | try setpath($item[0];$item[1]) "
                    "catch $before)"
                )
            else:
                expression = (
                    f"reduce {items_json}[] as $item (. ;"
                    ". as $before | try (if (getpath($item[1]) | "
                    "has($item[2])) then setpath($item[0];$item[3]) "
                    "else . end) catch $before)"
                )
        expression = _surge_safe_jq(expression)
        return [("[Body Rewrite]", f"{surge_type} {pattern} '{expression}'")]

    body_action = re.fullmatch(r"(request|response)-body-replace-regex", action)
    if body_action and value:
        return [
            (
                "[Body Rewrite]",
                f"http-{body_action.group(1)} {pattern} {value}",
            )
        ]

    header_action = re.fullmatch(
        r"(?:(request|response)-)?(header-(?:add|del|replace|replace-regex))",
        action,
    )
    if header_action and value:
        http_type = header_action.group(1) or "request"
        return [
            (
                "[Header Rewrite]",
                f"http-{http_type} {pattern} {header_action.group(2)} {value}",
            )
        ]
    raise ValueError(f"unsupported rewrite rule: {line}")


def _convert_rule(
    line: str, rewrites: dict[str, list[str]], arguments: dict[str, str]
) -> str:
    if line.startswith("^"):
        for heading, converted in _convert_rewrite(line, arguments):
            rewrites[heading].append(converted)
        return f"# Moved from invalid Loon [Rule]: {line}"

    match = re.fullmatch(
        r"(.*?),\s*([A-Z][A-Z0-9_-]*)(\s*,\s*(?:no-resolve|"
        r"extended-matching|pre-matching)(?:\s*,\s*(?:no-resolve|"
        r"extended-matching|pre-matching))*)?(\s*//.*)?",
        line,
    )
    if not match:
        return f"# Invalid upstream Loon rule: {line}"

    body, policy, options, comment = match.groups()
    options = options or ""
    comment = comment or ""
    if policy == "DIRECT":
        return f"{body}, DIRECT{options}{comment}"
    if policy in {"REJECT", "REJECT-DROP"}:
        return f"{body}, REJECT-DROP{options}{comment}"
    if policy == "PROXY":
        return f"# Requires main-profile policy selection: {line}"
    if policy in {"REJECT-DICT", "REJECT-IMG"}:
        rule_type, separator, pattern = body.partition(",")
        if rule_type.strip() == "URL-REGEX" and separator:
            action = "reject-dict" if policy == "REJECT-DICT" else "reject-img"
            for heading, converted in _convert_rewrite(
                f"{pattern.strip()} {action}", arguments
            ):
                rewrites[heading].append(converted)
            return f"# Converted to rewrite: {line}"
        return f"{body}, REJECT-DROP{options}{comment}"
    return f"# Unsupported Loon policy {policy}: {line}"


def _convert_script(
    line: str,
    arguments: dict[str, str],
    argument_kinds: dict[str, str],
    used_names: dict[str, int],
) -> tuple[list[str], str, str | None]:
    script_type, separator, remainder = line.partition(" ")
    if not separator or script_type not in {
        "http-request",
        "http-response",
        "cron",
        "generic",
    }:
        raise ValueError(f"unsupported script rule: {line}")

    pattern = None
    cron = None
    if script_type in {"http-request", "http-response"}:
        pattern, separator, raw_parameters = remainder.partition(" ")
    elif script_type == "cron":
        cron, separator, raw_parameters = remainder.partition(" script-path=")
        raw_parameters = "script-path=" + raw_parameters
    else:
        separator = " "
        raw_parameters = remainder
    if not separator:
        raise ValueError(f"invalid script rule: {line}")

    parameters = {}
    for parameter in _split_parameters(raw_parameters):
        if not parameter:
            continue
        key, found, value = parameter.partition("=")
        if not found:
            raise ValueError(f"invalid script parameter: {parameter}")
        parameters[key.strip()] = value.strip()

    script_path = parameters.get("script-path")
    if not script_path:
        raise ValueError(f"script-path is required: {line}")
    name = parameters.get("tag") or urlsplit(script_path).path.rsplit("/", 1)[-1]
    name = name.removesuffix(".js")
    used_names[name] = used_names.get(name, 0) + 1
    if used_names[name] > 1:
        name = f"{name} {used_names[name]}"

    options = [f"type={script_type}"]
    if pattern:
        pattern = f'"{pattern}"' if "," in pattern else pattern
        options.append(f"pattern={pattern}")
    if cron:
        cron = _replace_placeholders(cron.strip('"'), arguments)
        options.append(f'cronexp="{cron}"')
    options.append(f"script-path={script_path}")
    for key in ("requires-body", "binary-body-mode", "timeout"):
        if key in parameters:
            options.append(f"{key}={parameters[key]}")

    notes = []
    argument = parameters.get("argument")
    named_arguments = None
    if argument and argument.startswith("[") and argument.endswith("]"):
        items = _split_parameters(argument[1:-1])
        matches = [re.fullmatch(r"\{([^{}]+)\}", item) for item in items]
        if matches and all(matches):
            names = [match.group(1) for match in matches if match]
            if all(name in arguments for name in names):
                named_arguments = names
    enable = parameters.get("enable")
    if enable:
        enable_name = enable.strip("{}")
        if enable_name not in arguments:
            raise ValueError(f"undefined enable argument: {enable_name}")
        if named_arguments is not None:
            if enable_name not in named_arguments:
                named_arguments.append(enable_name)
        elif not argument and enable_name in arguments:
            named_arguments = [enable_name]
        elif argument:
            argument = _replace_placeholders(argument, arguments)
            enable = _replace_placeholders(enable, arguments)
            argument = f"[{argument},{enable}]"
        else:
            argument = _replace_placeholders(enable, arguments)
        notes.append(
            f"# Loon enable 参数 {arguments.get(enable_name, enable_name)} "
            "已作为同名脚本参数字段传入；脚本需在 false 时直接退出。"
        )
    if named_arguments is not None:
        style = _script_argument_style(script_path)
        if style == "query":
            argument = "&".join(
                f"{loon_name}=" + "{{{" + arguments[loon_name] + "}}}"
                for loon_name in named_arguments
            )
        else:
            fields = []
            for loon_name in named_arguments:
                surge_name = arguments[loon_name]
                value = "{{{" + surge_name + "}}}"
                if argument_kinds[loon_name] != "switch":
                    value = f'"{value}"'
                fields.append(
                    f'{json.dumps(loon_name, ensure_ascii=False)}:{value}'
                )
            argument = "{" + ",".join(fields) + "}"
            argument = '"' + argument.replace('"', r'\"') + '"'
            if style == "object":
                notes.append(
                    "# Surge $argument 为字符串；上游脚本需先执行 "
                    "JSON.parse($argument) 再读取同名字段。"
                )
    elif argument:
        argument = _replace_placeholders(argument, arguments)
    if argument:
        if not (argument.startswith('"') and argument.endswith('"')):
            argument = f'"{argument}"'
        options.append(f"argument={argument}")
    panel = None
    if script_type == "generic":
        panel_options = [
            f'title="{name}"',
            'content="点击刷新"',
            f"script-name={name}",
        ]
        if icon := parameters.get("img-url"):
            panel_options.extend([f"icon={icon}", "icon-color=#5d84f8"])
        panel = f"{name} = " + ",".join(panel_options)
    return notes, f"{name} = " + ",".join(options), panel


def _metadata(line: str) -> str | None:
    if not line.startswith("#!") or "=" not in line:
        return line
    key, value = line[2:].split("=", 1)
    if not value and key not in {"name", "desc"}:
        return None
    if key == "tag":
        return f"#!category={value}"
    if key == "system":
        return "#!system=ios" if "macOS" not in value else None
    if key in {"system_version", "loon_version"}:
        return None
    if key == "open":
        return f"#!openUrl={value}"
    if key in {"input", "select"}:
        return f"# Loon metadata: {line}"
    return line


def convert_lpx(
    source: str,
    source_url: str | None = None,
    unavailable_resources: set[str] | None = None,
) -> str:
    arguments, argument_kinds, argument_defaults, argument_notes = _arguments(
        source
    )
    metadata = []
    metadata_notes = []
    general = []
    rules = []
    scripts = []
    panels = []
    mitm = []
    rewrites = {
        "[URL Rewrite]": [],
        "[Map Local]": [],
        "[Header Rewrite]": [],
        "[Body Rewrite]": [],
    }
    used_script_names = {}
    section = None

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            if section not in {
                "[Argument]",
                "[General]",
                "[Rule]",
                "[Rewrite]",
                "[Script]",
                "[MitM]",
            }:
                raise ValueError(f"unsupported LPX section: {section}")
            continue

        if section is None:
            converted = _metadata(line)
            if converted is not None:
                target = metadata if converted.startswith("#!") else metadata_notes
                target.append(converted)
        elif section == "[Argument]":
            continue
        elif not stripped:
            continue
        elif section == "[General]":
            key, separator, value = stripped.partition("=")
            if not separator or key.strip() not in {"real-ip", "always-real-ip"}:
                raise ValueError(f"unsupported General setting: {stripped}")
            general.append(f"always-real-ip = %APPEND% {value.strip()}")
        elif section == "[Rule]":
            if stripped.startswith("#"):
                rules.append(stripped)
            else:
                rules.append(_convert_rule(stripped, rewrites, arguments))
        elif section == "[Rewrite]":
            if stripped.startswith("#"):
                continue
            for heading, converted in _convert_rewrite(stripped, arguments):
                rewrites[heading].append(converted)
        elif section == "[Script]":
            if stripped.startswith("#"):
                scripts.append(stripped)
            else:
                notes, converted, panel = _convert_script(
                    stripped, arguments, argument_kinds, used_script_names
                )
                scripts.extend([*notes, converted])
                if panel:
                    panels.append(panel)
        elif section == "[MitM]":
            if stripped.startswith("#"):
                mitm.append(stripped)
                continue
            key, separator, value = stripped.partition("=")
            if not separator or key.strip().lower() != "hostname":
                raise ValueError(f"unsupported MITM setting: {stripped}")
            mitm.append(f"hostname = %APPEND% {value.strip()}")

    if (
        rewrites["[Map Local]"] or rewrites["[Body Rewrite]"]
    ) and not any(line.startswith("#!requirement=") for line in metadata):
        metadata.append("#!requirement=CORE_VERSION>=20")
    if panels and not any(line.startswith("#!system=") for line in metadata):
        metadata.append("#!system=ios")

    name = next((line for line in metadata if line.startswith("#!name=")), None)
    if not name or not name.removeprefix("#!name="):
        raise ValueError("LPX plugin has no name")
    description = next(
        (
            line
            for line in metadata
            if line.startswith("#!desc=") and line.removeprefix("#!desc=")
        ),
        "#!desc=由 SelfSurge 从 Loon 插件转换。",
    )
    if any(
        rule.startswith("# Requires main-profile policy selection:")
        for rule in rules
    ):
        description += " 注意：PROXY 规则需在 Surge 主配置中手动指定策略。"
    if panels:
        description += " 注意：Surge Panel 不提供 Loon 的长按节点上下文。"
    category = next(
        (line for line in metadata if line.startswith("#!category=")),
        "#!category=其他",
    )
    output = [
        name,
        description,
        category,
        *(
            line
            for line in metadata
            if not line.startswith(("#!name=", "#!desc=", "#!category="))
        ),
    ]
    if argument_defaults:
        output.extend(
            [
                "#!arguments=" + ",".join(argument_defaults),
                "#!arguments-desc=Loon 选项与策略已转为 Surge 自由输入；"
                "脚本参数按同名字段传入，具体格式见模块注释；"
                "enable 字段为 false 时脚本需立即退出。",
            ]
        )

    notes = []
    if source_url:
        notes.extend(
            [
                f"# Source: {source_url}",
                "# Adapted from Loon LPX to Surge module by SelfSurge.",
                f"# Upstream LPX license: CC BY-NC-SA 4.0 {CC_LICENSE_URL}",
            ]
        )
    notes.extend(metadata_notes)
    notes.extend(argument_notes)
    while notes and not notes[0]:
        notes.pop(0)
    while notes and not notes[-1]:
        notes.pop()
    if notes:
        output.extend(["", *notes])

    while output and not output[-1]:
        output.pop()
    sections = [
        ("[General]", general),
        ("[Rule]", rules),
        *rewrites.items(),
        ("[Script]", scripts),
        ("[Panel]", panels),
        ("[MITM]", mitm),
    ]
    for heading, lines in sections:
        if lines:
            output.extend(["", heading, *lines])

    converted = "\n".join(output).rstrip() + "\n"
    unavailable_resources = unavailable_resources or set()
    for url in resource_urls(converted) - unavailable_resources:
        converted = converted.replace(url, published_resource_url(url))
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Loon LPX plugin to a Surge module."
    )
    parser.add_argument("url", help="URL of the LPX plugin")
    args = parser.parse_args()

    try:
        url = plugin_url(args.url)
        sys.stdout.write(convert_lpx(fetch_lpx(url), source_url=url))
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"selfsurge: {error}\n")


if __name__ == "__main__":
    main()
