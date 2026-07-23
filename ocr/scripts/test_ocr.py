#!/usr/bin/env python3
"""Unit tests for ocr.py pure helpers. Run: python3 scripts/test_ocr.py"""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ocr  # noqa: E402


class ResolvePaddleLang(unittest.TestCase):
    def test_composite_takes_primary(self):
        self.assertEqual(ocr.resolve_paddle_lang("rus+eng"), "ru")

    def test_eng(self):
        self.assertEqual(ocr.resolve_paddle_lang("eng"), "en")

    def test_chi_sim(self):
        self.assertEqual(ocr.resolve_paddle_lang("chi_sim"), "ch")

    def test_auto_defaults_en(self):
        self.assertEqual(ocr.resolve_paddle_lang("auto"), "en")

    def test_unknown_defaults_en(self):
        self.assertEqual(ocr.resolve_paddle_lang("xyz"), "en")


class ParsePaddleResult(unittest.TestCase):
    def _stub(self, texts, scores, polys):
        # PaddleOCR 3.x result item exposes attributes; emulate via SimpleNamespace
        return types.SimpleNamespace(rec_texts=texts, rec_scores=scores, rec_polys=polys)

    def test_basic_parse(self):
        # two lines, second one higher on the page (smaller y) than first
        poly_top = [[10, 5], [110, 5], [110, 25], [10, 25]]      # y=5
        poly_bottom = [[10, 60], [90, 60], [90, 80], [10, 80]]    # y=60
        item = self._stub(["world", "hello"], [0.90, 0.80],
                          [poly_bottom, poly_top])
        text, mean_conf, words = ocr._parse_paddle_result([item])
        # position-sorted top-to-bottom → hello then world
        self.assertEqual(text.splitlines(), ["hello", "world"])
        self.assertAlmostEqual(mean_conf, 85.0, places=1)
        self.assertEqual(len(words), 2)

    def test_bbox_from_poly(self):
        poly = [[10, 5], [110, 5], [110, 25], [10, 25]]
        item = self._stub(["x"], [0.5], [poly])
        _, _, words = ocr._parse_paddle_result([item])
        # bbox = [min_x, min_y, w, h]
        self.assertEqual(words[0]["bbox"], [10, 5, 100, 20])
        self.assertEqual(words[0]["conf"], 50)

    def test_empty(self):
        item = self._stub([], [], [])
        text, mean_conf, words = ocr._parse_paddle_result([item])
        self.assertEqual(text, "")
        self.assertEqual(mean_conf, 0.0)
        self.assertEqual(words, [])


class ResolveVisionConfig(unittest.TestCase):
    def test_ok(self):
        key, model, endpoint = ocr.resolve_vision_config(
            vision_api_key="k", vision_model="m", vision_api_url="http://x"
        )
        self.assertEqual((key, model, endpoint), ("k", "m", "http://x"))

    def test_empty_model_errors(self):
        with self.assertRaises(ocr.OcrError):
            ocr.resolve_vision_config(vision_api_key="k", vision_model="")

    def test_empty_key_errors(self):
        with self.assertRaises(ocr.OcrError):
            ocr.resolve_vision_config(vision_api_key="", vision_model="m")

    def test_does_not_read_env(self):
        os.environ["OPENAI_API_KEY"] = "should-not-be-used"
        try:
            with self.assertRaises(ocr.OcrError):
                ocr.resolve_vision_config(vision_api_key="", vision_model="m")
        finally:
            del os.environ["OPENAI_API_KEY"]


class Regression(unittest.TestCase):
    def test_parse_page_range(self):
        self.assertEqual(ocr._parse_page_range("1-3,5", 10), [1, 2, 3, 5])
        self.assertEqual(ocr._parse_page_range("1-3,5", 4), [1, 2, 3])

    def test_resolve_preprocess_image_none(self):
        caps = types.SimpleNamespace(has_cv2=True, has_numpy=True)
        self.assertEqual(ocr.resolve_preprocess("auto", None, caps, "image"), "none")

    def test_resolve_preprocess_explicit(self):
        caps = types.SimpleNamespace(has_cv2=False, has_numpy=False)
        self.assertEqual(ocr.resolve_preprocess("full", None, caps, "pdf"), "full")


class FatalRaisesOcrError(unittest.TestCase):
    """`_fatal` used to call sys.exit() directly, which killed the whole host
    process when ocr.py was imported as a library. It must raise instead."""

    def test_raises_ocr_error_with_code(self):
        with self.assertRaises(ocr.OcrError) as ctx:
            ocr._fatal("boom", ocr.EXIT_MISSING_BINARY)
        self.assertEqual(str(ctx.exception), "boom")
        self.assertEqual(ctx.exception.code, ocr.EXIT_MISSING_BINARY)

    def test_default_code_is_bad_args(self):
        with self.assertRaises(ocr.OcrError) as ctx:
            ocr._fatal("boom")
        self.assertEqual(ctx.exception.code, ocr.EXIT_BAD_ARGS)


class RecognizeOptionsDefaults(unittest.TestCase):
    """Guard against silent drift between library defaults and prior CLI defaults."""

    def test_defaults_match_former_argparse_defaults(self):
        options = ocr.RecognizeOptions()
        self.assertEqual(options.engine, "auto")
        self.assertEqual(options.lang, "auto")
        self.assertEqual(options.dpi, 0)
        self.assertEqual(options.preprocess, "auto")
        self.assertEqual(options.psm, ocr.DEFAULT_PSM)
        self.assertEqual(options.min_conf, ocr.DEFAULT_MIN_CONF)
        self.assertFalse(options.no_cleanup)
        self.assertFalse(options.force)
        self.assertIsNone(options.timeout)


class ProcessFileGuards(unittest.TestCase):
    def test_unsupported_extension_raises_ocr_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = os.path.join(tmp, "file.docx")
            with open(bogus, "w", encoding="utf-8") as f:
                f.write("x")
            caps = ocr.Caps()
            cache = ocr.Cache(None)
            with self.assertRaises(ocr.OcrError) as ctx:
                ocr.process_file(bogus, ocr.RecognizeOptions(), caps, cache, tmp)
            self.assertEqual(ctx.exception.code, ocr.EXIT_UNSUPPORTED)

    def test_recognize_rejects_vision_engine(self):
        with self.assertRaises(ocr.OcrError):
            ocr.recognize("whatever.png", ocr.RecognizeOptions(engine="vision"))


class VisionApiTimeoutKwarg(unittest.TestCase):
    """`timeout=None` must mean "SDK default", not an explicit None passed to
    the OpenAI client (which would disable the client's own default timeout).
    """

    def _install_fake_openai(self, captured: dict) -> None:
        class FakeMessage:
            content = "recognized text"

        class FakeChoice:
            message = FakeMessage()

        class FakeCompletion:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                return FakeCompletion()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.chat = FakeChat()

        fake_module = types.ModuleType("openai")
        fake_module.OpenAI = FakeOpenAI
        sys.modules["openai"] = fake_module

    def _call_vision_api(self, **kwargs) -> dict:
        captured: dict = {}
        self._install_fake_openai(captured)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                png = os.path.join(tmp, "p.png")
                with open(png, "wb") as f:
                    f.write(b"\x89PNG\r\n\x1a\n")
                ocr.vision_api(
                    [(1, png)],
                    vision_api_url="http://x",
                    vision_api_key="k",
                    vision_model="m",
                    **kwargs,
                )
        finally:
            del sys.modules["openai"]
        return captured

    def test_omits_timeout_kwarg_when_none(self):
        captured = self._call_vision_api(timeout=None)
        self.assertNotIn("timeout", captured)

    def test_passes_timeout_kwarg_when_set(self):
        captured = self._call_vision_api(timeout=12.5)
        self.assertEqual(captured.get("timeout"), 12.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
