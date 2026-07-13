"""Tests for veto_agents.agents.adbuyer.creative.brand.

Covers (no network, no LLM key required):
  - save/load/clear roundtrip
  - BRAND_PROFILE_PATH env override
  - missing / empty / corrupt YAML → load() returns None (never raises)
  - HTML strip + metadata/hex harvest from a fixture string
  - extract() with a mocked structured_llm (stamps source + extracted_at)
  - director injection with / without a brand file (monkeypatched load)
  - prompt_block / summary_line rendering
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from veto_agents.agents.adbuyer.creative import brand
from veto_agents.agents.adbuyer.creative.brand import BrandError, BrandProfile


def _sample() -> BrandProfile:
    return BrandProfile(
        name="Veto",
        product="Payment policy enforcement for AI agents",
        one_liner="The guard that blocks agent spends before they happen",
        audience="Developers running autonomous agents with wallets",
        tone="confident, technical, direct",
        aesthetic="light mode, cyan/cream, clean and minimal",
        value_props=["hard-stops, not advisory warnings", "signed receipts"],
        voice_dos=["plain english", "concrete numbers"],
        voice_donts=["hype words", "exclamation marks"],
        forbidden=["gambling imagery", "competitor names"],
        colors={"primary": "#06b6d4", "background": "#fffdf5"},
        source={"url": "https://veto-ai.com"},
    )


class _TmpBrand(unittest.TestCase):
    """Base: point BRAND_PROFILE_PATH at a throwaway file for the whole test."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "brand.yaml"
        self._prev = os.environ.get("BRAND_PROFILE_PATH")
        os.environ["BRAND_PROFILE_PATH"] = str(self.path)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BRAND_PROFILE_PATH", None)
        else:
            os.environ["BRAND_PROFILE_PATH"] = self._prev
        self._dir.cleanup()


class TestRoundtrip(_TmpBrand):
    def test_env_override_used(self):
        self.assertEqual(brand.brand_path(), self.path)

    def test_save_load_clear_roundtrip(self):
        p = _sample()
        saved_path = brand.save_brand(p)
        self.assertEqual(saved_path, self.path)
        self.assertTrue(self.path.exists())
        # Header comment is present so a human knows the file is editable.
        self.assertIn("# Veto brand profile", self.path.read_text())

        loaded = brand.load_brand()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, "Veto")
        self.assertEqual(loaded.tone, "confident, technical, direct")
        self.assertEqual(loaded.colors["primary"], "#06b6d4")
        self.assertEqual(loaded.forbidden, ["gambling imagery", "competitor names"])
        self.assertEqual(loaded.source.get("url"), "https://veto-ai.com")

        self.assertTrue(brand.clear())
        self.assertFalse(self.path.exists())
        self.assertIsNone(brand.load_brand())
        # clear() on an absent file is a no-op that returns False.
        self.assertFalse(brand.clear())

    def test_save_omits_empty_fields(self):
        p = BrandProfile(name="X", product="Y", tone="t")
        brand.save_brand(p)
        # Inspect the YAML body only (the header comment mentions field names).
        body = "\n".join(
            ln for ln in self.path.read_text().splitlines() if not ln.startswith("#")
        )
        self.assertIn("name: X", body)
        self.assertNotIn("value_props", body)  # empty list omitted
        self.assertNotIn("colors", body)       # empty dict omitted

    def test_design_aliases(self):
        self.assertIs(brand.load, brand.load_brand)
        self.assertIs(brand.save, brand.save_brand)
        self.assertIs(brand.prompt_block, brand.brand_prompt_block)
        self.assertIs(brand.summary_line, brand.brand_summary_line)


class TestLoadDefensive(_TmpBrand):
    def test_missing_file_returns_none(self):
        self.assertIsNone(brand.load_brand())

    def test_empty_file_returns_none(self):
        self.path.write_text("")
        self.assertIsNone(brand.load_brand())

    def test_corrupt_yaml_returns_none(self):
        self.path.write_text("name: [unterminated\n  : : :")
        self.assertIsNone(brand.load_brand())  # must NOT raise

    def test_non_mapping_yaml_returns_none(self):
        self.path.write_text("- just\n- a\n- list\n")
        self.assertIsNone(brand.load_brand())

    def test_identity_less_profile_treated_as_absent(self):
        self.path.write_text("tone: friendly\n")
        self.assertIsNone(brand.load_brand())


class TestHtmlHarvest(unittest.TestCase):
    FIXTURE = (
        "<html><head>"
        "<title>Veto — block agent spends</title>"
        '<meta name="description" content="The guard for AI agents.">'
        '<meta property="og:title" content="Veto">'
        '<meta name="theme-color" content="#06b6d4">'
        "<style>body{background:#fffdf5;color:#0f172a}.btn{color:#06b6d4}</style>"
        "<script>var x = 1; document.write('IGNORE ME');</script>"
        "</head><body>"
        '<div style="border:1px solid #06b6d4">'
        "<h1>Veto</h1><p>Hard-stops, not advisory warnings.</p>"
        "</div></body></html>"
    )

    def test_strip_removes_script_style_tags(self):
        out = brand._strip_html(self.FIXTURE)
        self.assertNotIn("IGNORE ME", out)          # script content gone
        self.assertNotIn("background:#fffdf5", out)  # style content gone
        self.assertNotIn("<", out)                   # no tags
        self.assertIn("Hard-stops", out)             # real copy kept
        self.assertIn("Veto", out)

    def test_harvest_meta_lines(self):
        lines = brand._harvest_meta(self.FIXTURE)
        joined = "\n".join(lines)
        self.assertIn("PAGE TITLE: Veto — block agent spends", joined)
        self.assertIn("META DESCRIPTION: The guard for AI agents.", joined)
        self.assertIn("OG TITLE: Veto", joined)
        self.assertIn("THEME-COLOR: #06b6d4", joined)
        self.assertIn("HEX COLORS FOUND IN CSS:", joined)
        # deduped hexes from <style> + style="" attrs
        self.assertIn("#06b6d4", joined)
        self.assertIn("#fffdf5", joined)
        self.assertIn("#0f172a", joined)

    def test_thin_guard(self):
        self.assertTrue(brand._thin("   short   "))
        self.assertFalse(brand._thin("x" * 200))


class TestExtract(_TmpBrand):
    LLM_OUT = {
        "name": "Veto",
        "product": "Payment policy enforcement for AI agents",
        "one_liner": "Block agent spends before they happen",
        "audience": "Developers running autonomous agents",
        "tone": "confident, technical, direct",
        "value_props": ["hard-stops"],
        "colors": {"primary": "#06b6d4"},
    }

    def test_extract_from_url_stamps_source(self):
        cfg = object()
        with mock.patch.object(brand, "_fetch_url", return_value="PAGE TEXT " * 50), \
             mock.patch.object(brand, "structured_llm", return_value=dict(self.LLM_OUT)) as m:
            profile = brand.extract_from_url("https://veto-ai.com", cfg)
        self.assertEqual(profile.name, "Veto")
        self.assertEqual(profile.colors["primary"], "#06b6d4")
        self.assertEqual(profile.source["url"], "https://veto-ai.com")
        self.assertIn("extracted_at", profile.source)
        # exactly one structured_llm call, forced tool name.
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.kwargs["tools_name"], "emit_brand")

    def test_extract_dispatches_file_vs_url(self):
        cfg = object()
        # a non-http source routes to file extraction
        with mock.patch.object(brand, "extract_from_file") as mf, \
             mock.patch.object(brand, "extract_from_url") as mu:
            brand.extract("/tmp/brand.md", cfg)
            mf.assert_called_once()
            mu.assert_not_called()
        with mock.patch.object(brand, "extract_from_file") as mf, \
             mock.patch.object(brand, "extract_from_url") as mu:
            brand.extract("https://x.com", cfg)
            mu.assert_called_once()
            mf.assert_not_called()

    def test_extract_from_file_reads_and_extracts(self):
        cfg = object()
        dump = Path(self._dir.name) / "brand.md"
        dump.write_text("Veto is payment policy enforcement for AI agents. " * 10)
        with mock.patch.object(brand, "structured_llm", return_value=dict(self.LLM_OUT)):
            profile = brand.extract_from_file(str(dump), cfg)
        self.assertEqual(profile.name, "Veto")
        self.assertEqual(profile.source["file"], str(dump))

    def test_extract_empty_profile_raises(self):
        cfg = object()
        with mock.patch.object(brand, "_fetch_url", return_value="text " * 50), \
             mock.patch.object(brand, "structured_llm", return_value={"tone": "x"}):
            with self.assertRaises(BrandError):
                brand.extract_from_url("https://veto-ai.com", cfg)

    def test_missing_file_raises_brand_error(self):
        with self.assertRaises(BrandError):
            brand.extract_from_file("/no/such/brand.md", object())


class TestPromptRendering(unittest.TestCase):
    def test_prompt_block_includes_key_directives(self):
        block = brand.brand_prompt_block(_sample())
        self.assertIn("BRAND PROFILE (binding", block)
        self.assertIn("Veto", block)
        self.assertIn("Audience:", block)
        self.assertIn("Voice DO:", block)
        self.assertIn("Voice DON'T:", block)
        self.assertIn("primary=#06b6d4", block)
        self.assertIn("MUST name these colors", block)
        self.assertIn("FORBIDDEN", block)
        self.assertIn("gambling imagery", block)

    def test_summary_line_is_one_line_clamped(self):
        line = brand.brand_summary_line(_sample())
        self.assertNotIn("\n", line)
        self.assertIn("Veto", line)
        self.assertIn("Never:", line)
        self.assertLessEqual(len(line), 201)


class TestDirectorInjection(_TmpBrand):
    """The director injects the brand block only when a brand exists — and its
    call to structured_llm is otherwise unchanged."""

    def _run_director(self):
        from veto_agents.agents.adbuyer.creative import director

        captured = {}

        def fake_llm(cfg, system, user, schema, *, tools_name, max_tokens=1024):
            captured["system"] = system
            captured["user"] = user
            return {
                "concept": "c", "copy": {"headlines": [], "primary_texts": [], "ctas": []},
                "image_prompt": "img", "video_prompt": "vid", "voiceover_script": "vo",
            }

        with mock.patch.object(director, "structured_llm", side_effect=fake_llm):
            director.direct("a great coffee brand", cfg=object())
        return captured

    def test_no_brand_file_leaves_prompt_brand_free(self):
        self.assertIsNone(brand.load_brand())
        cap = self._run_director()
        self.assertNotIn("BRAND PROFILE", cap["user"])
        self.assertIn("PRODUCT BRIEF", cap["user"])

    def test_with_brand_file_injects_binding_block(self):
        brand.save_brand(_sample())
        cap = self._run_director()
        self.assertIn("BRAND PROFILE (binding", cap["user"])
        self.assertIn("Veto", cap["user"])
        self.assertIn("BINDING", cap["system"])  # system-prompt directive present


if __name__ == "__main__":
    unittest.main()
