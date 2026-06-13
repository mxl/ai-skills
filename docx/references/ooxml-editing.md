# OOXML Editing Reference

Recipes for editing `.docx` XML directly after `scripts/safe-unpack.py`.
All facts derived from ECMA-376 and observable library behaviour.

## Workflow

```
safe-unpack.py  →  Edit XML with Edit tool  →  pack.py --original
```

- Use the **Edit tool** for targeted string replacements in unpacked XML files.
  Do not write one-off Python scripts for individual edits — direct edits are
  transparent, auditable, and don't leave throwaway files.
- `pack.py` auto-repairs trivial issues before writing: adds
  `xml:space="preserve"` to `<w:t>` with leading/trailing whitespace, and
  regenerates invalid `w:id` values.
- Run `validate.py` after every pack to catch structural problems early.

---

## Smart Quotes

When inserting new text, use XML entities for professional typography:

```xml
<w:t>Here&#x2019;s a quote: &#x201C;Hello&#x201D;</w:t>
```

| Entity | Character | Name |
|--------|-----------|------|
| `&#x2018;` | ' | left single quotation mark |
| `&#x2019;` | ' | right single / apostrophe |
| `&#x201C;` | " | left double quotation mark |
| `&#x201D;` | " | right double quotation mark |

---

## Schema Compliance

### Element order in `<w:pPr>`

The paragraph properties element requires this child order:

```xml
<w:pPr>
  <w:pStyle w:val="Heading1"/>
  <w:numPr>...</w:numPr>
  <w:spacing w:before="240" w:after="120"/>
  <w:ind w:left="720"/>
  <w:jc w:val="both"/>
  <w:rPr>...</w:rPr>   <!-- rPr always last -->
</w:pPr>
```

### Whitespace in `<w:t>`

Add `xml:space="preserve"` whenever the text has leading or trailing spaces:

```xml
<w:t xml:space="preserve"> leading or trailing space </w:t>
```

### RSIDs

Revision save IDs must be 8-digit uppercase hex strings, e.g. `00AB1234`.
`pack.py` auto-repair does not generate RSIDs; leave existing ones unchanged.

---

## Tracked Changes

### Insertion

```xml
<w:ins w:id="1" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r>
    <w:t>inserted text</w:t>
  </w:r>
</w:ins>
```

### Deletion

```xml
<w:del w:id="2" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r>
    <w:delText>deleted text</w:delText>
  </w:r>
</w:del>
```

Inside `<w:del>`:
- Use `<w:delText>` instead of `<w:t>`
- Use `<w:delInstrText>` instead of `<w:instrText>`

### Minimal diffs — only mark what changes

Split the run at the change boundary and wrap only the differing fragment:

```xml
<!-- Change "30 days" to "60 days" -->
<w:r><w:t xml:space="preserve">The term is </w:t></w:r>
<w:del w:id="1" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r><w:delText>30</w:delText></w:r>
</w:del>
<w:ins w:id="2" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r><w:t>60</w:t></w:r>
</w:ins>
<w:r><w:t xml:space="preserve"> days.</w:t></w:r>
```

### Replacing a whole run

Replace the entire `<w:r>` block — never inject revision tags inside a run.
Copy the original `<w:rPr>` into both the deletion and insertion runs to
preserve formatting (bold, font size, colour, etc.):

```xml
<!-- Original: <w:r><w:rPr><w:b/></w:rPr><w:t>old</w:t></w:r> -->
<w:del w:id="3" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r><w:rPr><w:b/></w:rPr><w:delText>old</w:delText></w:r>
</w:del>
<w:ins w:id="4" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r><w:rPr><w:b/></w:rPr><w:t>new</w:t></w:r>
</w:ins>
```

### Deleting an entire paragraph or list item

Mark the paragraph mark as deleted too — otherwise accepting the change leaves
an empty paragraph/list item:

```xml
<w:p>
  <w:pPr>
    <w:numPr>          <!-- keep list numbering if present -->
      <w:ilvl w:val="0"/>
      <w:numId w:val="1"/>
    </w:numPr>
    <w:rPr>
      <!-- paragraph mark deletion -->
      <w:del w:id="5" w:author="Agent" w:date="2026-06-12T00:00:00Z"/>
    </w:rPr>
  </w:pPr>
  <w:del w:id="6" w:author="Agent" w:date="2026-06-12T00:00:00Z">
    <w:r><w:delText>Entire paragraph content.</w:delText></w:r>
  </w:del>
</w:p>
```

### Rejecting another author's insertion

Nest your deletion inside their insertion. Do not modify their `<w:ins>`:

```xml
<w:ins w:id="10" w:author="Jane" w:date="2026-06-01T00:00:00Z">
  <w:del w:id="20" w:author="Agent" w:date="2026-06-12T00:00:00Z">
    <w:r><w:delText>Jane's inserted text</w:delText></w:r>
  </w:del>
</w:ins>
```

### Restoring another author's deletion

Add your insertion immediately after their `<w:del>`. Do not modify theirs:

```xml
<w:del w:id="10" w:author="Jane" w:date="2026-06-01T00:00:00Z">
  <w:r><w:delText>deleted text to restore</w:delText></w:r>
</w:del>
<w:ins w:id="21" w:author="Agent" w:date="2026-06-12T00:00:00Z">
  <w:r><w:t>deleted text to restore</w:t></w:r>
</w:ins>
```

### Author and date

Use `"Agent"` as the default author unless the user requests a specific name.
Use the current UTC datetime in ISO-8601 format: `2026-06-12T00:00:00Z`.

---

## Comments

Comments in modern DOCX require four coordinated XML parts plus the document
body markers. Each part needs a relationship entry and content-type declaration.

### The four comment parts

| Part | Purpose |
|------|---------|
| `word/comments.xml` | Comment text content |
| `word/commentsExtended.xml` | `paraIdParent` for threading (replies) |
| `word/commentsIds.xml` | Stable `paraId` / `durableId` per comment |
| `word/people.xml` | Author identity for the sidebar |

All four must be added to `word/_rels/document.xml.rels` and
`[Content_Types].xml`. Generate the boilerplate with Python rather than
copying templates from other sources.

### Minimal comments.xml

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="Agent" w:date="2026-06-12T00:00:00Z" w:initials="A">
    <w:p>
      <w:r><w:t>Comment text here.</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>
```

### Markers in document.xml

`commentRangeStart` and `commentRangeEnd` are **siblings of `<w:r>`** inside
`<w:p>` — never nested inside a run:

```xml
<w:p>
  <w:commentRangeStart w:id="0"/>
  <w:r><w:t>Commented text.</w:t></w:r>
  <w:commentRangeEnd w:id="0"/>
  <w:r>
    <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    <w:commentReference w:id="0"/>
  </w:r>
</w:p>
```

### Reply (nested comment)

A reply's `paraIdParent` in `commentsExtended.xml` points to the parent
comment's `paraId`. Nest the reply range markers inside the parent range:

```xml
<!-- document.xml -->
<w:p>
  <w:commentRangeStart w:id="0"/>
    <w:commentRangeStart w:id="1"/>
    <w:r><w:t>Text with comment and reply.</w:t></w:r>
    <w:commentRangeEnd w:id="1"/>
  <w:commentRangeEnd w:id="0"/>
  <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    <w:commentReference w:id="0"/></w:r>
  <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    <w:commentReference w:id="1"/></w:r>
</w:p>
```

### Using python-docx 1.2 for comments (preferred)

For simple add-comment tasks, use the native API — no XML boilerplate needed:

```python
from docx import Document

doc = Document("input.docx")
para = doc.paragraphs[0]
run = para.runs[0]
doc.add_comment(run, "Comment text", author="Agent", initials="A")
doc.save("output.docx")
```

Use the OOXML approach when you need replies, resolved state, or when editing
the unpacked XML directly.

---

## Images in Existing Documents

To insert an image into an existing document via the unpack/edit/pack workflow:

**1. Copy the image into `word/media/`:**
```
unpacked/word/media/image1.png
```

**2. Add a relationship to `word/_rels/document.xml.rels`:**
```xml
<Relationship Id="rId10"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="media/image1.png"/>
```

**3. Add a content-type entry to `[Content_Types].xml`** (if the extension is
not already covered by a `<Default>` element):
```xml
<Default Extension="png" ContentType="image/png"/>
```

**4. Add the drawing markup to `document.xml`:**
```xml
<w:p>
  <w:r>
    <w:drawing>
      <wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
        <wp:extent cx="3200400" cy="2400300"/>  <!-- EMU: 914400 = 1 inch -->
        <wp:docPr id="1" name="Image 1"/>
        <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:nvPicPr>
                <pic:cNvPr id="1" name="image1.png"/>
                <pic:cNvPicPr/>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="rId10"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
              </pic:blipFill>
              <pic:spPr>
                <a:xfrm>
                  <a:ext cx="3200400" cy="2400300"/>
                </a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
```

**EMU conversion:** 914,400 EMU = 1 inch. For a 3.5 × 2.6 inch image:
`cx = 3200400`, `cy = 2400300` (approximate).
