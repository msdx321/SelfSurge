import unittest

from selfsurge import convert_lpx, fetch_lpx


BLOCK_ADVERTISERS_URL = (
    "https://kelee.one/Tool/Loon/Lpx/BlockAdvertisers.lpx"
)
SOUL_INSTALL_URL = (
    "loon://import?plugin="
    "https://kelee.one/Tool/Loon/Lpx/Soul_remove_ads.lpx"
)


class BlockAdvertisersTest(unittest.TestCase):
    def test_download_and_convert(self) -> None:
        module = convert_lpx(fetch_lpx(BLOCK_ADVERTISERS_URL))

        self.assertIn("#!name=广告平台拦截器", module)
        self.assertIn("[Rule]", module)
        self.assertIn("[URL Rewrite]", module)
        self.assertIn(
            r"^https:\/\/video-dsp\.pddpic\.com\/market-dsp-video\/ _ reject",
            module,
        )
        self.assertIn("hostname = %APPEND% video-dsp.pddpic.com, ", module)


class SoulTest(unittest.TestCase):
    def test_import_url_and_complex_sections(self) -> None:
        module = convert_lpx(fetch_lpx(SOUL_INSTALL_URL))

        self.assertIn("#!name=Soul去广告", module)
        self.assertIn("[Map Local]", module)
        self.assertIn('data-type=text data="{}"', module)
        self.assertIn('data-type=text data="" status-code=200', module)
        self.assertIn("[Body Rewrite]", module)
        self.assertIn("'del(.data)'", module)
        self.assertIn("[Script]", module)
        self.assertIn(
            "移除Soul广告 = type=http-response,pattern=", module
        )
        self.assertIn(
            "script-path=https://kelee.one/Resource/JavaScript/Soul/"
            "Soul_remove_ads.js,requires-body=true",
            module,
        )
        self.assertIn(
            "hostname = %APPEND% api*.soulapp.cn, ", module
        )


if __name__ == "__main__":
    unittest.main()
