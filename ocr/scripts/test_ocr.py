#!/usr/bin/env python3
"""Unit tests for ocr.py pure helpers. Run: python3 scripts/test_ocr.py"""

import os
import sys
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
    def _args(self, key="", model="", endpoint=""):
        return types.SimpleNamespace(
            vision_api_key=key, vision_model=model, vision_api_url=endpoint
        )

    def test_ok(self):
        key, model, endpoint = ocr.resolve_vision_config(
            self._args(key="k", model="m", endpoint="http://x")
        )
        self.assertEqual((key, model, endpoint), ("k", "m", "http://x"))

    def test_empty_model_errors(self):
        with self.assertRaises(SystemExit):
            ocr.resolve_vision_config(self._args(key="k", model=""))

    def test_empty_key_errors(self):
        with self.assertRaises(SystemExit):
            ocr.resolve_vision_config(self._args(key="", model="m"))

    def test_does_not_read_env(self):
        os.environ["OPENAI_API_KEY"] = "should-not-be-used"
        try:
            with self.assertRaises(SystemExit):
                ocr.resolve_vision_config(self._args(key="", model="m"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
