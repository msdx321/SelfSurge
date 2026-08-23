import argparse
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


LOON_USER_AGENT = "Loon/1022 CFNetwork/1498.700.2 Darwin/23.6.0"


def _plugin_url(value: str) -> str:
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


def fetch_lpx(url: str) -> str:
    request = Request(_plugin_url(url), headers={"User-Agent": LOON_USER_AGENT})
    with urlopen(request, timeout=30) as response:
        source = response.read().decode("utf-8-sig")

    if not any(line.startswith("#!name=") for line in source.splitlines()):
        raise ValueError("response is not an LPX plugin")

    return source


def _convert_rewrite(line: str) -> tuple[str, str]:
    parts = line.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError(f"invalid rewrite rule: {line}")

    pattern, action = parts[:2]
    argument = parts[2] if len(parts) == 3 else None

    if action == "reject" and argument is None:
        return "[URL Rewrite]", f"{pattern} _ reject"
    if action == "reject-dict" and argument is None:
        return (
            "[Map Local]",
            f'{pattern} data-type=text data="{{}}" '
            'header="Content-Type:application/json" status-code=200',
        )
    if action == "reject-200" and argument is None:
        return "[Map Local]", f'{pattern} data-type=text data="" status-code=200'
    if action == "response-body-json-del" and argument == "data":
        return "[Body Rewrite]", f"http-response-jq {pattern} 'del(.data)'"

    raise ValueError(f"unsupported rewrite rule: {line}")


def _convert_script(line: str) -> str:
    parts = line.split(maxsplit=2)
    if len(parts) != 3 or parts[0] not in {"http-request", "http-response"}:
        raise ValueError(f"unsupported script rule: {line}")

    script_type, pattern, raw_parameters = parts
    parameters = []
    name = None
    script_path = None

    for parameter in raw_parameters.split(","):
        key, separator, value = parameter.strip().partition("=")
        if not separator:
            raise ValueError(f"invalid script parameter: {parameter}")
        if key == "tag":
            name = value
        else:
            parameters.append(f"{key}={value}")
            if key == "script-path":
                script_path = value

    if script_path is None:
        raise ValueError(f"script-path is required: {line}")
    if name is None:
        name = urlsplit(script_path).path.rsplit("/", 1)[-1].removesuffix(".js")

    options = ",".join(
        [f"type={script_type}", f"pattern={pattern}", *parameters]
    )
    return f"{name} = {options}"


def convert_lpx(source: str) -> str:
    output = []
    section = None
    rewrites = {
        "[URL Rewrite]": [],
        "[Map Local]": [],
        "[Body Rewrite]": [],
    }

    def flush_rewrites() -> None:
        for heading, rules in rewrites.items():
            if not rules:
                continue
            if output and output[-1]:
                output.append("")
            output.extend([heading, *rules, ""])

    for line in source.splitlines():
        stripped = line.strip()

        if stripped == "[Rewrite]":
            section = "rewrite"
            continue

        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "rewrite":
                flush_rewrites()
            section = stripped.lower()
            output.append("[MITM]" if section == "[mitm]" else stripped)
            continue

        if section == "rewrite":
            if stripped and not stripped.startswith("#"):
                heading, converted = _convert_rewrite(stripped)
                rewrites[heading].append(converted)
            continue

        if section == "[script]" and stripped and not stripped.startswith("#"):
            line = _convert_script(stripped)
        elif section == "[mitm]" and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().lower() == "hostname":
                line = f"hostname = %APPEND% {value.strip()}"

        output.append(line)

    if section == "rewrite":
        flush_rewrites()

    return "\n".join(output).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Loon LPX plugin to a Surge module."
    )
    parser.add_argument("url", help="URL of the LPX plugin")
    args = parser.parse_args()

    try:
        sys.stdout.write(convert_lpx(fetch_lpx(args.url)))
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"selfsurge: {error}\n")


if __name__ == "__main__":
    main()
