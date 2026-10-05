import argparse
import base64
import json
import re
import sys
from typing import NamedTuple
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


def fetch_bytes(url: str, user_agent: str = LOON_USER_AGENT) -> bytes:
    request = Request(url, headers={"User-Agent": user_agent})
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


def _script_argument_style(
    url: str, script_sources: dict[str, bytes | None]
) -> str:
    try:
        if url not in script_sources:
            script_sources[url] = fetch_bytes(url)
        content = script_sources[url]
        if content is None:
            raise ValueError(f"cannot inspect script arguments: unavailable script {url}")
        script = content.decode("utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot inspect script arguments for {url}: {error}") from error
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


class _Regex(NamedTuple):
    pattern: str
    flags: str


class _Variable(NamedTuple):
    name: str


class _ArgumentSet(NamedTuple):
    names: tuple[str, ...]


_V2_REGEX = re.compile(r"/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\[])*)/([A-Za-z]*)")
_V2_LITERAL = re.compile(r"-?\d+(?:\.\d+)?(?![\w.])|(?:true|false|null)\b")
_V2_CALL = re.compile(r"\s*([A-Za-z_][\w.]*)\s*\(")
_V2_OPTION = re.compile(r"\s*([A-Za-z_]\w*)\s*=")


def _v2_skip(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _v2_value(text: str, index: int) -> tuple[object, int]:
    """Parse one Loon v2 literal, regex, variable, array or argument set."""
    index = _v2_skip(text, index)
    char = text[index:index + 1]
    if char == '"':
        end = index + 1
        while end < len(text) and text[end] != '"':
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise ValueError("unterminated string")
        return json.loads(text[index:end + 1]), end + 1
    if char == "`":
        end = text.find("`", index + 1)
        if end == -1:
            raise ValueError("unterminated raw string")
        return text[index + 1:end], end + 1
    if char == "/":
        match = _V2_REGEX.match(text, index)
        if not match:
            raise ValueError("unterminated regex literal")
        return _Regex(match[1], match[2]), match.end()
    if text.startswith("${", index):
        end = text.find("}", index)
        if end == -1:
            raise ValueError("unterminated variable")
        return _Variable(text[index + 2:end]), end + 1
    if char in {"[", "{"}:
        items, index = _v2_list(text, index + 1, "]" if char == "[" else "}")
        if char == "[":
            return items, index
        if not items or not all(isinstance(item, _Variable) for item in items):
            raise ValueError("argument object must list plugin arguments")
        return _ArgumentSet(tuple(item.name for item in items)), index
    match = _V2_LITERAL.match(text, index)
    if not match:
        raise ValueError(f"unsupported value: {text[index:index + 40]}")
    return json.loads(match[0]), match.end()


def _v2_list(text: str, index: int, closing: str) -> tuple[list, int]:
    items = []
    index = _v2_skip(text, index)
    if text.startswith(closing, index):
        return items, index + 1
    while True:
        value, index = _v2_value(text, index)
        items.append(value)
        index = _v2_skip(text, index)
        if text.startswith(closing, index):
            return items, index + 1
        if not text.startswith(",", index):
            raise ValueError(f"expected ',' or '{closing}': {text[index:index + 40]}")
        index += 1


def _v2_actions(text: str) -> tuple[list[tuple[str, list]], dict[str, object]]:
    """Parse `action(args) | action(args) [with key=value, ...]`."""
    actions = []
    index = 0
    while True:
        match = _V2_CALL.match(text, index)
        if not match:
            raise ValueError(f"expected an action: {text[index:index + 40]}")
        values, index = _v2_list(text, match.end(), ")")
        actions.append((match[1], values))
        index = _v2_skip(text, index)
        if not text.startswith("|", index):
            break
        index += 1

    options = {}
    if match := re.compile(r"with\s").match(text, index):
        index = match.end()
        while True:
            option = _V2_OPTION.match(text, index)
            if not option:
                raise ValueError(f"invalid with option: {text[index:index + 40]}")
            options[option[1]], index = _v2_value(text, option.end())
            index = _v2_skip(text, index)
            if not text.startswith(",", index):
                break
            index += 1
    if index != len(text):
        raise ValueError(f"unexpected text: {text[index:index + 40]}")
    return actions, options


def _dynamic(value) -> bool:
    if isinstance(value, (_Variable, _ArgumentSet)):
        return True
    if isinstance(value, list):
        return any(map(_dynamic, value))
    return isinstance(value, str) and "${" in value


def _surge_regex(pattern: str, flags: str) -> str:
    if set(flags) - set("ims") or len(set(flags)) != len(flags):
        raise ValueError(f"unsupported regex flags: {flags}")
    # Surge uses whitespace to separate rewrite fields.
    pattern = re.sub(r"\s", lambda match: rf"\x{ord(match[0]):02x}", pattern)
    return f"(?{flags}){pattern}" if flags else pattern


def _batch(values: list, count: int, operation: str) -> list[tuple]:
    """Expand Loon's scalar or equal-length array argument forms."""
    if len(values) != count:
        raise ValueError(f"{operation} expects {count} argument(s)")
    if not isinstance(values[0], list):
        return [tuple(values)]
    if not values[0] or not all(
        isinstance(value, list) and len(value) == len(values[0])
        for value in values
    ):
        raise ValueError(f"{operation} batch arrays must be non-empty and equal length")
    return list(zip(*values))


def _field(value) -> str:
    """Return a whitespace-free Surge rewrite field."""
    if isinstance(value, _Regex):
        return _surge_regex(*value)
    if isinstance(value, bool) or value is None:
        value = json.dumps(value)
    elif isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value or re.search(r"\s", value):
        raise ValueError(f"value cannot be a Surge rewrite field: {value!r}")
    return value


_JQ_TOKEN = re.compile(
    r"\s+|\#[^\n]*|(?:[.$]?[A-Za-z_][A-Za-z_0-9]*)(?:::[A-Za-z_][A-Za-z_0-9]*)*"
    r"|\?//|//=|//|[|+*/%=-]=|!=|<=|>=|\.\.|.",
    re.DOTALL,
)


def _jq_tokens(value: str) -> list[str]:
    """Keep fields, variables, operators and quoted strings as distinct tokens."""
    tokens = []
    index = 0
    while index < len(value):
        if value[index] != '"':
            match = _JQ_TOKEN.match(value, index)
            tokens.append(match[0])
            index = match.end()
            continue

        start = index
        index += 1
        # A string may contain nested strings inside \(jq interpolation).
        contexts = ['"']
        while contexts and index < len(value):
            char = value[index]
            if contexts[-1] == '"':
                if value.startswith(r"\(", index):
                    contexts.append(")")
                    index += 2
                    continue
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    contexts.pop()
            elif char == "#":
                newline = value.find("\n", index)
                index = len(value) if newline == -1 else newline
                continue
            elif char == '"':
                contexts.append('"')
            elif char == "(":
                contexts.append(")")
            elif char == ")":
                contexts.pop()
            index += 1
        if contexts:
            raise ValueError("unterminated JQ string or interpolation")
        tokens.append(value[start:index])
    return tokens


def _normalize_jq(value: str) -> str:
    output = []
    conditions = []
    tokens = _jq_tokens(value)
    significant = [
        token for token in tokens
        if not token.isspace() and not token.startswith("#")
    ]
    position = 0
    for token in tokens:
        if token.isspace() or token.startswith("#"):
            output.append(token)
            continue
        previous = significant[position - 1] if position else None
        position += 1
        following = significant[position] if position < len(significant) else None
        if (
            token == ".end" and previous == "else" and conditions
            and following in {None, ";", ")", "]", "}"}
        ):
            # Legacy Loon filters use `else .end` at the end of a branch.
            # Only split it where a real field access would leave if unclosed.
            output.append(".")
            token = "end"
        object_key = following == ":" or (
            previous in {"{", ","} and following in {"}", ","}
        )
        keyword = False
        if not object_key:
            if token == "if":
                conditions.append(False)
                keyword = True
            elif token == "else" and conditions:
                conditions[-1] = True
                keyword = True
            elif token == "end" and conditions:
                if not conditions.pop():
                    output.append(" else . ")
                keyword = True
            elif token in {"then", "elif", "and", "or"}:
                keyword = True
        if keyword and output and not output[-1].isspace():
            output.append(" ")
        output.append(token)
        if keyword:
            output.append(" ")
    return "".join(output).strip()


def _jq_alternative_start(tokens: list[str]) -> int:
    """Find the left operand of // without crossing lower-precedence syntax."""
    closing = {")": "(", "]": "[", "}": "{", "end": "if"}
    boundaries = {
        "(", "[", "{", "|", ",", ";", ":", "//", "if", "then", "elif", "else"
    }
    nested = []
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token in closing and (
            token != "end" or not nested or nested[-1] == "if"
        ):
            nested.append(closing[token])
        elif nested:
            if token == nested[-1]:
                nested.pop()
        elif token in boundaries:
            return index + 1
    return 0


def _surge_safe_jq(value: str) -> str:
    output = []
    for token in _jq_tokens(value):
        if token.isspace() or token.startswith("#"):
            if output and not output[-1].isspace():
                output.append(" ")
            continue
        if token in {";", "//", "//="}:
            while output and output[-1].isspace():
                output.pop()
            if token == "//" and output and output[-1] == "?":
                # Surge treats whitespace + // as an inline comment, but
                # removing that whitespace creates jq's distinct ?// operator.
                # Group the left operand instead, preserving its precedence.
                output.insert(_jq_alternative_start(output), "(")
                output.append(")")
        output.append(token)
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


def _conditional_parts(line: str) -> tuple[str, str, str | None, str] | None:
    """Parse one URL condition without absorbing extra conditions into its regex."""
    if not re.match(r"(?:request|response)\s+if\b", line):
        return None
    match = re.fullmatch(
        r"(request|response)\s+if\s+\$\{url\}\s*~=\s*"
        r"/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\[])*)/([A-Za-z]*)"
        r"(?:\s+as\s+([A-Za-z_]\w*))?\s+then\s+(.+)",
        line,
    )
    if not match:
        raise ValueError("only a single URL regex condition can be converted")
    direction, pattern, flags, capture, action = match.groups()
    return direction, _surge_regex(pattern, flags), capture, action


def _json_rewrite(
    direction: str, pattern: str, operation: str, entries: list
) -> tuple[str, str]:
    """Build a jq rewrite that skips paths missing from the actual body."""
    if operation == "del":
        paths_json = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        expression = (
            f"reduce {paths_json}[] as $path (. ;"
            ". as $before | try delpaths([$path]) catch $before)"
        )
    elif operation == "add":
        items_json = json.dumps(
            [[path, value] for path, value in entries],
            ensure_ascii=False, separators=(",", ":"),
        )
        expression = (
            f"reduce {items_json}[] as $item (. ;"
            ". as $before | try setpath($item[0];$item[1]) "
            "catch $before)"
        )
    else:
        items_json = json.dumps(
            [[path, path[:-1], path[-1], value] for path, value in entries],
            ensure_ascii=False, separators=(",", ":"),
        )
        expression = (
            f"reduce {items_json}[] as $item (. ;"
            ". as $before | try (if (getpath($item[1]) | "
            "has($item[2])) then setpath($item[0];$item[3]) "
            "else . end) catch $before)"
        )
    expression = _surge_safe_jq(expression)
    return "[Body Rewrite]", f"http-{direction}-jq {pattern} '{expression}'"


def _conditional_status(value) -> int:
    # Loon accepts 100–599, but Surge Map Local cannot represent 1xx responses.
    if type(value) is not int or not 200 <= value <= 599:
        raise ValueError(f"status cannot be represented by Surge Map Local: {value}")
    return value


_CONTENT_TYPES = {
    "json": "application/json",
    "text": "text/plain",
    "plain": "text/plain",
    "css": "text/css",
    "html": "text/html",
    "javascript": "application/javascript",
    "png": "image/png",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "tiff": "image/tiff",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
}


def _content_type(kind) -> str:
    if kind not in _CONTENT_TYPES:
        raise ValueError(f"unsupported mock content type: {kind}")
    return _CONTENT_TYPES[kind]


def _conditional_response(
    pattern: str, kind: str, data: str, status: int, encoded: bool = False
) -> tuple[str, str]:
    content_type = _content_type(kind)
    if encoded:
        base64.b64decode(data, validate=True)
    else:
        data = base64.b64encode(data.encode()).decode()
    return (
        "[Map Local]",
        f'{pattern} data-type=base64 data="{data}" '
        f'header="Content-Type:{content_type}" status-code={status}',
    )


def _json_value(value):
    # Hub migrated legacy `replace data {}` rules to string values such as
    # "{}"; keep the container semantics the legacy syntax had.
    if isinstance(value, str) and value.strip()[:1] in {"{", "["}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _conditional_rewrite(
    line: str, arguments: dict[str, str]
) -> list[tuple[str, str]] | None:
    conditional = _conditional_parts(line)
    if conditional is None:
        return None
    direction, pattern, capture, action = conditional
    actions, options = _v2_actions(action)
    if options:
        raise ValueError("rewrite actions do not accept with options")
    return [
        converted
        for operation, values in actions
        for converted in _rewrite_action(
            direction, pattern, capture, operation, values, arguments
        )
    ]


def _rewrite_action(
    direction: str,
    pattern: str,
    capture: str | None,
    operation: str,
    values: list,
    arguments: dict[str, str],
) -> list[tuple[str, str]]:
    if direction == "request" and operation in {"redirect", "url.replace"}:
        status = "header"
        if operation == "redirect":
            if (
                len(values) != 2
                or type(values[0]) is not int
                or values[0] not in {302, 307}
            ):
                raise ValueError(f"invalid redirect arguments: {values}")
            status, values = values[0], values[1:]
        if len(values) != 1 or not isinstance(values[0], str):
            raise ValueError(f"invalid URL replacement: {values}")

        def replace_variable(match: re.Match) -> str:
            name = match[1]
            if name in arguments:
                return "{{{" + arguments[name] + "}}}"
            if capture and re.fullmatch(re.escape(capture) + r"\.\d+", name):
                return "$" + name.rsplit(".", 1)[1]
            raise ValueError(f"unsupported URL replacement variable: {name}")

        target = re.sub(r"\$\{([^{}]+)\}", replace_variable, values[0])
        if not target or re.search(r"\s", target):
            raise ValueError(
                f"URL replacement must not be empty or contain whitespace: {target}"
            )
        return [("[URL Rewrite]", f"{pattern} {target} {status}")]

    if _dynamic(values):
        raise ValueError(f"dynamic arguments are unsupported for {operation}")

    if direction == "request" and operation in {
        "reject", "reject_dict", "reject_array", "reject_img"
    }:
        if len(values) not in ({1, 2} if operation == "reject" else {1}):
            raise ValueError(f"invalid {operation} arguments: {values}")
        status = _conditional_status(values[0])
        if operation == "reject_img":
            return [("[Map Local]", f"{pattern} data-type=tiny-gif status-code={status}")]
        if operation == "reject":
            data = values[1] if len(values) == 2 else ""
            if not isinstance(data, str):
                raise ValueError(f"reject body must be a string: {values}")
            if data:
                return [_conditional_response(pattern, "text", data, status)]
        else:
            data = "{}" if operation == "reject_dict" else "[]"
        header = (
            ' header="Content-Type:application/json"'
            if operation != "reject" else ""
        )
        return [(
            "[Map Local]",
            f'{pattern} data-type=text data="{data}"{header} status-code={status}',
        )]

    target, _, method = operation.partition(".")
    if target != direction:
        raise ValueError(f"unsupported {direction} action: {operation}")

    if method in {"body.mock", "body.mock_file"} and direction == "response":
        if not 2 <= len(values) <= 4 or not all(
            isinstance(value, str) for value in values[:2]
        ):
            raise ValueError(f"invalid {operation} arguments: {values}")
        kind, data = values[:2]
        status = _conditional_status(values[2] if len(values) >= 3 else 200)
        encoded = values[3] if len(values) == 4 else False
        if not isinstance(encoded, bool):
            raise ValueError(f"mock base64 flag must be a boolean: {values}")
        if method == "body.mock":
            return [_conditional_response(pattern, kind, data, status, encoded)]
        if encoded or urlsplit(data).scheme not in {"http", "https"}:
            raise ValueError(f"mock file must be a plain HTTP(S) resource: {data}")
        return [(
            "[Map Local]",
            f'{pattern} data-type=file data="{data}" '
            f'header="Content-Type:{_content_type(kind)}" status-code={status}',
        )]

    if method == "json.jq" and len(values) == 1 and isinstance(values[0], str):
        return [("[Body Rewrite]", f"http-{direction}-jq {pattern} {_jq(values[0])}")]
    if method == "json.jq_file" and len(values) == 1 and isinstance(values[0], str):
        jq = _jq(f'jq-path="{values[0]}"')
        return [("[Body Rewrite]", f"http-{direction}-jq {pattern} {jq}")]
    if method == "json.delete":
        paths = [path for path, in _batch(values, 1, operation)]
        if not all(isinstance(path, str) and path for path in paths):
            raise ValueError(f"invalid {operation} key paths: {values}")
        return [_json_rewrite(
            direction, pattern, "del", [_json_path(path) for path in paths]
        )]
    if method in {"json.add", "json.replace"}:
        pairs = _batch(values, 2, operation)
        if not all(isinstance(path, str) and path for path, _ in pairs):
            raise ValueError(f"invalid {operation} key paths: {values}")
        return [_json_rewrite(
            direction, pattern, method.removeprefix("json."),
            [(_json_path(path), _json_value(value)) for path, value in pairs],
        )]

    if method == "body.replace":
        rewrites = []
        for regex, replacement in _batch(values, 2, operation):
            if isinstance(regex, str):
                regex = _Regex(re.escape(regex), "")
            if isinstance(replacement, str):
                # Spaces are written as \x20, as in legacy Loon replacements.
                replacement = replacement.replace(" ", r"\x20")
            rewrites.append((
                "[Body Rewrite]",
                f"http-{direction} {pattern} {_field(regex)} {_field(replacement)}",
            ))
        return rewrites

    header_counts = {"add": 2, "set": 2, "del": 1, "replace": 3}
    header_method = method.removeprefix("header.")
    if method.startswith("header.") and header_method in header_counts:
        rewrites = []
        for fields in _batch(values, header_counts[header_method], operation):
            name, *rest = map(_field, fields)
            prefix = f"http-{direction} {pattern}"
            if header_method in {"del", "set"}:
                rewrites.append(f"{prefix} header-del {name}")
            if header_method in {"add", "set"}:
                rewrites.append(f"{prefix} header-add {name} {rest[0]}")
            if header_method == "replace":
                rewrites.append(f"{prefix} header-replace-regex {name} {' '.join(rest)}")
        return [("[Header Rewrite]", rewrite) for rewrite in rewrites]

    raise ValueError(f"unsupported conditional action or arguments: {operation}{values}")


def _convert_rewrite(
    line: str, arguments: dict[str, str]
) -> list[tuple[str, str]]:
    conditional = _conditional_rewrite(line, arguments)
    if conditional is not None:
        return conditional

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
            entries = [_json_path(word.replace(r"\x20", " ")) for word in words]
        else:
            if len(words) % 2:
                raise ValueError(f"invalid JSON rewrite pairs: {line}")
            entries = [
                (_json_path(key.replace(r"\x20", " ")), _loon_value(raw_value))
                for key, raw_value in zip(words[::2], words[1::2])
            ]
        return [_json_rewrite(http_type, pattern, operation, entries)]

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


def _v2_script(
    line: str, arguments: dict[str, str]
) -> tuple[str, str | None, str | None, dict[str, str]] | None:
    """Parse a Loon v2 script entry into the legacy parameter form."""
    pattern = cron = None
    conditional = _conditional_parts(line)
    if conditional:
        direction, pattern, _, action = conditional
        script_type = f"http-{direction}"
    else:
        match = re.fullmatch(
            r"(cron|generic|network-changed)\s+(?:(.+?)\s+)?then\s+(.+)", line
        )
        if not match:
            return None
        script_type, trigger, action = match.groups()
        if (script_type == "cron") != (trigger is not None):
            raise ValueError(f"invalid {script_type} trigger: {line}")
        if trigger:
            value, end = _v2_value(trigger, 0)
            if end != len(trigger):
                raise ValueError(f"invalid cron expression: {trigger}")
            if isinstance(value, _Variable):
                if value.name not in arguments:
                    raise ValueError(f"undefined cron argument: {value.name}")
                cron = "{" + value.name + "}"
            elif isinstance(value, str):
                cron = value
            else:
                raise ValueError(f"invalid cron expression: {trigger}")

    actions, options = _v2_actions(action)
    if len(actions) != 1 or actions[0][0] != "script":
        raise ValueError(f"expected a single script action: {action}")
    values = actions[0][1]
    if not 1 <= len(values) <= 2 or not isinstance(values[0], str) or not values[0]:
        raise ValueError(f"invalid script arguments: {values}")
    parameters = {"script-path": values[0]}
    if len(values) == 2:
        argument = values[1]
        if isinstance(argument, _ArgumentSet):
            if undefined := set(argument.names) - set(arguments):
                raise ValueError(f"undefined script arguments: {sorted(undefined)}")
            parameters["argument"] = (
                "[" + ",".join("{" + name + "}" for name in argument.names) + "]"
            )
        elif isinstance(argument, str):
            parameters["argument"] = '"' + argument.replace('"', r'\"') + '"'
        else:
            raise ValueError(f"invalid script argument: {argument}")

    for key, value in options.items():
        if key == "enable":
            if isinstance(value, _Variable):
                parameters["enable"] = "{" + value.name + "}"
            elif value is not True:
                raise ValueError(f"unsupported script enable value: {value}")
        elif key in {"tag", "img_url"} and isinstance(value, str):
            parameters[key.replace("_", "-")] = value
        elif key == "timeout" and type(value) in {int, float} and value > 0:
            parameters[key] = str(value)
        elif key in {"requires_body", "binary_body_mode", "debug"} and isinstance(
            value, bool
        ):
            parameters[key.replace("_", "-")] = json.dumps(value)
        else:
            raise ValueError(f"unsupported script option: {key}={value!r}")
    return script_type, pattern, cron, parameters


def _legacy_script(
    line: str,
) -> tuple[str, str | None, str | None, dict[str, str]]:
    script_type, separator, remainder = line.partition(" ")
    if not separator or script_type not in {
        "http-request",
        "http-response",
        "cron",
        "generic",
        "network-changed",
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

    return script_type, pattern, cron, parameters


def _convert_script(
    line: str,
    arguments: dict[str, str],
    argument_kinds: dict[str, str],
    used_names: dict[str, int],
    script_sources: dict[str, bytes | None],
) -> tuple[list[str], str, str | None]:
    v2 = _v2_script(line, arguments)
    if v2:
        script_type, pattern, cron, parameters = v2
    else:
        script_type, pattern, cron, parameters = _legacy_script(line)

    script_path = parameters.get("script-path")
    if not script_path:
        raise ValueError(f"script-path is required: {line}")
    name = parameters.get("tag") or urlsplit(script_path).path.rsplit("/", 1)[-1]
    name = name.removesuffix(".js")
    used_names[name] = used_names.get(name, 0) + 1
    if used_names[name] > 1:
        name = f"{name} {used_names[name]}"

    options = (
        ["type=event", "event-name=network-changed"]
        if script_type == "network-changed" else [f"type={script_type}"]
    )
    if pattern:
        pattern = f'"{pattern}"' if "," in pattern else pattern
        options.append(f"pattern={pattern}")
    if cron:
        cron = _replace_placeholders(cron.strip('"'), arguments)
        options.append(f'cronexp="{cron}"')
    options.append(f"script-path={script_path}")
    for key in ("requires-body", "binary-body-mode", "timeout", "debug"):
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
        style = _script_argument_style(script_path, script_sources)
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
    script_sources: dict[str, bytes | None] | None = None,
) -> str:
    if script_sources is None:
        script_sources = {}
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

    for line_number, line in enumerate(source.splitlines(), 1):
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
            try:
                converted_rules = _convert_rewrite(stripped, arguments)
            except ValueError as error:
                raise ValueError(
                    f"{source_url or '<input>'}:{line_number} {section}: "
                    f"{error}\n  {stripped}"
                ) from error
            for heading, converted in converted_rules:
                rewrites[heading].append(converted)
        elif section == "[Script]":
            if stripped.startswith("#"):
                scripts.append(stripped)
            else:
                try:
                    notes, converted, panel = _convert_script(
                        stripped, arguments, argument_kinds, used_script_names,
                        script_sources,
                    )
                except ValueError as error:
                    raise ValueError(
                        f"{source_url or '<input>'}:{line_number} {section}: "
                        f"{error}\n  {stripped}"
                    ) from error
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
