# OOXML / PresentationML Editing Reference

Recipes for editing `.pptx` XML directly after `scripts/safe-unpack.py`.
All facts derived from ECMA-376 and observable library behaviour.

## Workflow

```
safe-unpack.py  →  Edit XML with Edit tool  →  pack.py --original
```

- Use the **Edit tool** for targeted string replacements in unpacked XML files.
  Do not write one-off Python scripts for individual edits.
- `pack.py` auto-repairs `xml:space="preserve"` on `<a:t>` with leading/trailing whitespace.
- Run `validate.py` after every pack.

---

## Package Structure

A `.pptx` is a ZIP. Key parts:

```text
[Content_Types].xml
_rels/.rels
ppt/presentation.xml               # slide list (p:sldId entries → r:id refs)
ppt/_rels/presentation.xml.rels    # maps r:id → slide/layout/master/theme
ppt/slides/slide1.xml              # slide content
ppt/slides/_rels/slide1.xml.rels   # slide relationships (layout, media, notes)
ppt/slideLayouts/slideLayout1.xml  # layout template
ppt/slideMasters/slideMaster1.xml  # master template
ppt/theme/theme1.xml               # colours, fonts, effects
ppt/notesSlides/notesSlide1.xml    # speaker notes
docProps/core.xml                  # metadata (author, created, modified)
docProps/app.xml                   # app metadata (slide count, company)
```

---

## Key Namespaces

```python
NAMESPACES = {
    "p":      "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a":      "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r":      "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel":    "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct":     "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp":     "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc":     "http://purl.org/dc/elements/1.1/",
    "dcterms":"http://purl.org/dc/terms/",
    "app":    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "c":      "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "pic":    "http://schemas.openxmlformats.org/drawingml/2006/picture",
}
```

---

## Slide / Layout / Master Inheritance

```
slide.xml
  └─ slideLayout (via slide _rels: type=slideLayout)
       └─ slideMaster (via layout _rels: type=slideMaster)
            └─ theme (via master _rels: type=theme)
```

Placeholders in a slide (`<p:ph type="..." idx="..."/>`) inherit formatting
from the layout, then master, when no local override is present.

---

## Slide XML Skeleton

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
      <!-- shapes: <p:sp>, <p:pic>, <p:graphicFrame> -->
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
```

---

## Text Shape (Placeholder)

```xml
<p:sp>
  <p:nvSpPr>
    <p:cNvPr id="2" name="Title 1"/>
    <p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>
    <p:nvPr><p:ph type="title"/></p:nvPr>
  </p:nvSpPr>
  <p:spPr/>
  <p:txBody>
    <a:bodyPr/>
    <a:lstStyle/>
    <a:p>
      <a:r>
        <a:rPr lang="en-US" dirty="0"/>
        <a:t>Slide Title</a:t>
      </a:r>
    </a:p>
  </p:txBody>
</p:sp>
```

Placeholder types: `title`, `body`, `subTitle`, `dt` (date), `ftr` (footer),
`sldNum` (slide number), `pic`, `tbl`, `chart`, `clipArt`, `obj`.

---

## EMU Positioning

914,400 EMU = 1 inch. All `x`, `y`, `cx`, `cy` attributes are in EMU.

Standard widescreen (16:9) slide: `cx="9144000"` (10 in), `cy="5143500"` (5.625 in).
Standard 4:3 slide: `cx="9144000"`, `cy="6858000"` (7.5 in).

Shape position example (0.5 in from left, 0.3 in from top; 9 in wide, 1.25 in tall):

```xml
<p:spPr>
  <a:xfrm>
    <a:off x="457200" y="274638"/>
    <a:ext cx="8229600" cy="1143000"/>
  </a:xfrm>
  <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
</p:spPr>
```

---

## Adding a New Slide

**Step 1** — create `ppt/slides/slideN.xml` (copy/adapt skeleton above).

**Step 2** — add to `ppt/_rels/presentation.xml.rels`:
```xml
<Relationship Id="rId10"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
  Target="slides/slide3.xml"/>
```

**Step 3** — register in `ppt/presentation.xml` `<p:sldIdLst>`:
```xml
<p:sldId id="260" r:id="rId10"/>
```
`id` must be unique and > 255.

**Step 4** — add `[Content_Types].xml` Override:
```xml
<Override PartName="/ppt/slides/slide3.xml"
  ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
```

**Step 5** — create `ppt/slides/_rels/slide3.xml.rels` with at least a slideLayout relationship:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
    Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
```

---

## Speaker Notes

Notes slide is a separate part. Add relationship in slide's `_rels`:
```xml
<Relationship Id="rId2"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
  Target="../notesSlides/notesSlide1.xml"/>
```

Notes slide XML contains `<p:sp>` with `<p:ph type="body"/>` for notes text:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
         xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
         xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld>
    <p:spTree>
      <!-- slide image placeholder omitted for brevity -->
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="3" name="Notes Placeholder 2"/>
          <p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>
          <p:nvPr><p:ph type="body" idx="1"/></p:nvPr>
        </p:nvSpPr>
        <p:spPr/>
        <p:txBody>
          <a:bodyPr/>
          <a:lstStyle/>
          <a:p><a:r><a:t>Speaker notes text here.</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:notes>
```

Add content-type Override:
```xml
<Override PartName="/ppt/notesSlides/notesSlide1.xml"
  ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>
```

---

## Images in Slides

**1.** Copy image to `ppt/media/image1.png`.

**2.** Add relationship in slide's `_rels`:
```xml
<Relationship Id="rId3"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="../media/image1.png"/>
```

**3.** Add content-type Default (if not already present):
```xml
<Default Extension="png" ContentType="image/png"/>
```

**4.** Add `<p:pic>` in slide's `<p:spTree>`:
```xml
<p:pic>
  <p:nvPicPr>
    <p:cNvPr id="5" name="image1.png" descr="Alt text here"/>
    <p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>
    <p:nvPr/>
  </p:nvPicPr>
  <p:blipFill>
    <a:blip r:embed="rId3"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
    <a:stretch><a:fillRect/></a:stretch>
  </p:blipFill>
  <p:spPr>
    <a:xfrm>
      <a:off x="914400" y="914400"/>
      <a:ext cx="2743200" cy="2057400"/>
    </a:xfrm>
    <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
  </p:spPr>
</p:pic>
```

---

## Smart Quotes in Text

Use XML entities for professional typography:

| Entity | Character |
|--------|-----------|
| `&#x2018;` | ' left single |
| `&#x2019;` | ' right single / apostrophe |
| `&#x201C;` | " left double |
| `&#x201D;` | " right double |

---

## Whitespace in `<a:t>`

Add `xml:space="preserve"` when text has leading or trailing spaces:
```xml
<a:t xml:space="preserve"> leading or trailing space </a:t>
```
`pack.py` applies this auto-repair automatically.

---

## Editing Presentation-Level Properties

Slide dimensions in `ppt/presentation.xml`:
```xml
<p:sldSz cx="9144000" cy="5143500" type="screen16x9"/>
```
Common types: `screen16x9`, `screen4x3`, `custom`.

Default text style, colour maps, and embedded fonts are also in
`ppt/presentation.xml` — edit carefully to avoid breaking master inheritance.
