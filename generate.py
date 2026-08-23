from pathlib import Path

from selfsurge import convert_lpx, fetch_lpx, fetch_text


ROOT = Path(__file__).parent
PLUGINS = {
    "BlockAdvertisers.sgmodule": (
        "https://kelee.one/Tool/Loon/Lpx/BlockAdvertisers.lpx"
    ),
    "Soul_remove_ads.sgmodule": (
        "loon://import?plugin="
        "https://kelee.one/Tool/Loon/Lpx/Soul_remove_ads.lpx"
    ),
}
SCRIPTS = {
    "Soul/Soul_remove_ads.js": (
        "https://kelee.one/Resource/JavaScript/Soul/Soul_remove_ads.js"
    ),
}


def main() -> None:
    modules = {
        name: convert_lpx(fetch_lpx(url)) for name, url in PLUGINS.items()
    }
    scripts = {name: fetch_text(url) for name, url in SCRIPTS.items()}

    for name, content in modules.items():
        path = ROOT / "modules" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    for name, content in scripts.items():
        path = ROOT / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
