# common/ooxml — Shared OOXML Engine

Format-agnostic utilities and unpack/pack/validate engine shared by the
`docx` and `pptx` skills (and any future OOXML-based skill).

## Modules

| Module | Contents |
|--------|----------|
| `_bootstrap.py` | Locate repo root; add to `sys.path`; fix package-name shadowing |
| `zipsafe.py` | `zip_safety_report`, `safe_member_path`, `ZIP_LIMITS` |
| `xmlutil.py` | `pretty_print_xml`, `condense_xml`, `parse_xml_bytes`, shared namespaces |
| `io.py` | `sha256_file`, `emit_json`, `fail` |
| `engine.py` | `FormatProfile`, `unpack`, `pack`, `validate`, `CheckResult` |

## `FormatProfile`

The engine is parameterized by a `FormatProfile` dataclass. Each skill
defines its own profile with format-specific hooks:

```python
from dataclasses import dataclass
from common.ooxml.engine import FormatProfile, CheckResult
import zipfile

@dataclass
class MyProfile(FormatProfile):
    def pre_write_transform(self, name: str, data: bytes) -> bytes:
        # docx: merge adjacent runs; pptx: pass-through
        return data

    def autorepair(self, name: str, data: bytes) -> tuple[bytes, list[str]]:
        # Format-specific XML repair
        return data, []

    def extra_checks(self, zf: zipfile.ZipFile) -> list[CheckResult]:
        # Format-specific validation rules
        return []
```

## Bootstrap / import

Skills locate this package via `_bootstrap.install(__file__)`, which walks up
to the repo root (identified by `common/` + `README.md` siblings) or reads
`AI_SKILLS_ROOT` env var.

```python
# At the TOP of any skill script, before other imports:
from common.ooxml._bootstrap import install as _install
_install(__file__)

# Then use normally:
from common.ooxml.engine import FormatProfile, unpack, pack, validate
```

The bootstrap also removes the skill's `scripts/` and skill root from
`sys.path` to prevent shadowing of third-party packages (`docx`, `pptx`).

## Symlink installs

Per-skill symlink installs (`ln -s ai-skills/docx ~/.claude/skills/docx`)
require that the repo root also be reachable. Options:

1. Set `AI_SKILLS_ROOT=/path/to/ai-skills` in your environment.
2. Install skills by symlinking the whole repo into the skills path and
   configuring the skill loader to scan it recursively.
3. For Claude Code: symlink the individual skill dir AND set `AI_SKILLS_ROOT`.

## What stays format-specific (not in this package)

- `detect_format()` — returns `docx/docm/doc` vs `pptx/pptm/ppt`
- WordprocessingML namespaces (`w:`, `w14:`, `w15:`, `wp:`)
- PresentationML namespaces (`p:`, `p14:`)
- Run-merging logic (docx: `w:r` / `w:rPr` / `w:t`)
- Tracked-changes / comments validation (docx)
- Slide ↔ presentation.xml.rels consistency (pptx)
- `inspect.py`, `extract.py`, `sanitize.py`, `convert.py` per skill
