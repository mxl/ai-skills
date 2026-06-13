# docx-js Reference

npm package `docx` v9.x — JavaScript/TypeScript DOCX generation.

Install: `npm install -g docx`

---

## Minimal skeleton

```javascript
const {
  Document, Packer, Paragraph, TextRun,
  HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat,
  Header, Footer, PageNumber, PageBreak, PageOrientation,
  ExternalHyperlink, InternalHyperlink, Bookmark,
  ImageRun,
  FootnoteReferenceRun,
  TableOfContents,
  TabStopType, TabStopPosition,
  PositionalTab, PositionalTabAlignment, PositionalTabRelativeTo, PositionalTabLeader,
  Column, SectionType,
} = require('docx');
const fs = require('fs');

const doc = new Document({
  // Styles override: must use exact IDs to override built-in Word styles
  styles: {
    default: {
      document: { run: { font: "Arial", size: 24 } },  // 12pt default body
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1",
        basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, font: "Arial", color: "000000" },
        paragraph: {
          spacing: { before: 240, after: 120 },
          outlineLevel: 0,   // REQUIRED for TOC to pick up H1
        },
      },
      {
        id: "Heading2", name: "Heading 2",
        basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: "000000" },
        paragraph: {
          spacing: { before: 180, after: 90 },
          outlineLevel: 1,   // REQUIRED for TOC
        },
      },
    ],
  },

  // Numbering config — define once, reference everywhere
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "\u2022",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
      {
        reference: "numbers",
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
    ],
  },

  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },             // US Letter (DXA)
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }, // 1 inch
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({ children: [new TextRun("Header")] })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          children: [
            new TextRun("Page "),
            new TextRun({ children: [PageNumber.CURRENT] }),
            new TextRun(" of "),
            new TextRun({ children: [PageNumber.TOTAL_PAGES] }),
          ],
        })],
      }),
    },
    children: [
      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Title")] }),
      new Paragraph({ children: [new TextRun("Body text.")] }),
    ],
  }],
});

Packer.toBuffer(doc).then(buf => fs.writeFileSync("output.docx", buf));
```

---

## Critical Rules

These rules must always be followed. Violations cause silent breakage in Word
or Google Docs.

### Page size

docx-js defaults to **A4**. For US documents, always set explicitly:

```javascript
page: {
  size: { width: 12240, height: 15840 },   // US Letter
  margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
}
// Content width = 12240 - 1440 - 1440 = 9360 DXA
```

Common sizes (DXA; 1440 DXA = 1 inch):

| Paper | Width | Height |
|-------|-------|--------|
| US Letter | 12,240 | 15,840 |
| A4 | 11,906 | 16,838 |
| Legal | 12,240 | 20,160 |

**Landscape:** pass portrait dimensions and add `orientation`:
```javascript
size: { width: 12240, height: 15840, orientation: PageOrientation.LANDSCAPE }
// docx-js swaps width/height in the XML; content width = 15840 - margins
```

### Tables — dual widths and DXA only

```javascript
const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };

new Table({
  width: { size: 9360, type: WidthType.DXA },   // MUST equal sum of columnWidths
  columnWidths: [5000, 4360],                    // MUST sum to table width
  rows: [
    new TableRow({
      children: [
        new TableCell({
          width: { size: 5000, type: WidthType.DXA }, // MUST match columnWidths[0]
          borders,
          shading: { fill: "E8F0FE", type: ShadingType.CLEAR }, // CLEAR, not SOLID
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          children: [new Paragraph({ children: [new TextRun("Cell")] })],
        }),
        new TableCell({
          width: { size: 4360, type: WidthType.DXA },
          borders,
          shading: { fill: "FFFFFF", type: ShadingType.CLEAR },
          margins: { top: 80, bottom: 80, left: 120, right: 120 },
          children: [new Paragraph({ children: [new TextRun("Cell")] })],
        }),
      ],
    }),
  ],
})
```

Rules:
- **Always `WidthType.DXA`** — `WidthType.PERCENTAGE` breaks Google Docs rendering.
- Table `width.size` must equal the sum of `columnWidths`.
- Each cell `width.size` must match the corresponding `columnWidths` entry.
- Cell `margins` are internal padding; they reduce content area, not add to cell width.
- **`ShadingType.CLEAR`** — never use `ShadingType.SOLID` for coloured backgrounds
  (renders as solid black in some viewers).
- Do not use single-row tables as horizontal rules — empty cells have minimum height
  and render as boxes. Use a paragraph border instead:
  ```javascript
  new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "2E75B6", space: 1 } },
    children: [],
  })
  ```

### Lists — never unicode bullets as text

```javascript
// WRONG — inserts literal bullet character
new Paragraph({ children: [new TextRun("• Item")] })

// CORRECT — references numbering config defined on Document
new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  children: [new TextRun("Bullet item")],
})
new Paragraph({
  numbering: { reference: "numbers", level: 0 },
  children: [new TextRun("Numbered item")],
})
```

Numbering continuity:
- Same `reference` → continuous numbering (1, 2, 3 … 4, 5, 6)
- Different `reference` → restarts from 1

### PageBreak

Must be inside a `Paragraph`; a standalone `PageBreak` element creates invalid XML:

```javascript
// CORRECT
new Paragraph({ children: [new PageBreak()] })

// Also correct
new Paragraph({ pageBreakBefore: true, children: [new TextRun("New page")] })
```

### Never use `\n` in TextRun

Use separate `Paragraph` elements; `\n` inside a `TextRun` does not create a
paragraph break in Word.

### Images

`ImageRun` requires an explicit `type` field:

```javascript
new ImageRun({
  type: "png",                          // Required: png | jpg | jpeg | gif | bmp | svg
  data: fs.readFileSync("image.png"),
  transformation: { width: 200, height: 150 },
  altText: { title: "Chart", description: "Sales chart", name: "sales-chart" },
})
```

### Table of Contents

TOC works only with `HeadingLevel` enum. Custom styles on heading paragraphs
break it. The `outlineLevel` property in the style override is required:

```javascript
new TableOfContents("Table of Contents", {
  hyperlink: true,
  headingStyleRange: "1-3",
})
```

### Style overrides

Use the exact built-in IDs to override Word's built-in paragraph styles:
`"Heading1"`, `"Heading2"`, … `"Heading9"`, `"Normal"`, `"Title"`.
Include `outlineLevel` (0 for H1, 1 for H2, …) for TOC to function.

### Two-column footer layout

Do not use a table in a header or footer for two-column layouts — the table
minimum cell height creates visible empty boxes. Use tab stops instead:

```javascript
new Paragraph({
  children: [
    new TextRun("Company Name"),
    new TextRun({ children: ["\t", "January 2026"] }),
  ],
  tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
})
```

---

## Footnotes

```javascript
const doc = new Document({
  footnotes: {
    1: { children: [new Paragraph("See appendix A.")] },
    2: { children: [new Paragraph("Source: Annual Report 2025.")] },
  },
  sections: [{
    children: [
      new Paragraph({
        children: [
          new TextRun("Revenue grew 15%"),
          new FootnoteReferenceRun(1),
          new TextRun(" on an adjusted basis"),
          new FootnoteReferenceRun(2),
          new TextRun("."),
        ],
      }),
    ],
  }],
});
```

---

## Multi-Column Layout

```javascript
// Equal-width columns with a separator line
sections: [{
  properties: {
    column: { count: 2, space: 720, equalWidth: true, separate: true },
  },
  children: [ /* content flows across columns automatically */ ],
}]

// Custom-width columns
sections: [{
  properties: {
    column: {
      equalWidth: false,
      children: [
        new Column({ width: 5400, space: 720 }),
        new Column({ width: 3240 }),
      ],
    },
  },
  children: [],
}]
```

Force a column break:
```javascript
new Paragraph({
  children: [],
  // add a section with type NEXT_COLUMN before the next content
})
```

---

## Post-generation validation

Always run after generating:

```bash
python scripts/validate.py output.docx
```

If validation fails: unpack with `safe-unpack.py`, locate the invalid XML,
fix it, and repack with `pack.py`.
