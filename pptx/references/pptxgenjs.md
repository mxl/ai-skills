# PptxGenJS Reference

npm package `pptxgenjs` v4.x — JavaScript/TypeScript PPTX generation.

Install: `npm install -g pptxgenjs`

Use PptxGenJS as the **primary engine** for creating new decks, especially
data-driven or rich presentations. Fall back to `python-pptx` for simple decks
or when Node is not available.

---

## Minimal Skeleton

```javascript
const pptxgen = require("pptxgenjs");

const pres = new pptxgen();

// Always set layout before adding slides
pres.layout = "LAYOUT_WIDE";  // 13.33 × 7.5 inches (16:9)

// Define a slide master (optional but recommended)
pres.defineSlideMaster({
  title: "MASTER",
  background: { color: "FFFFFF" },
  objects: [
    {
      text: {
        text: "Company Name",
        options: { x: 0.3, y: 6.9, w: 5, h: 0.4, color: "888888", fontSize: 10 },
      },
    },
  ],
  slideNumber: { x: 12, y: 6.9, color: "888888", fontSize: 10 },
});

// Add slides
const slide = pres.addSlide({ masterName: "MASTER" });

slide.addText("Hello World", {
  x: 0.5, y: 2.0, w: 12.33, h: 1.5,
  fontSize: 40, bold: true, color: "003366",
  align: "center",
});

slide.addNotes("Speaker notes for this slide.");

// Save
pres.writeFile({ fileName: "output.pptx" });
```

---

## Critical Rules

### Slide Layout — Always Set Explicitly

PptxGenJS defaults to **10 × 7.5 inches (4:3)**. Always set before adding slides:

```javascript
// Built-in layouts
pres.layout = "LAYOUT_WIDE";     // 13.33 × 7.5 in (16:9) — recommended default
pres.layout = "LAYOUT_4x3";     // 10 × 7.5 in (4:3)
pres.layout = "LAYOUT_16x10";   // 10 × 6.25 in
pres.layout = "LAYOUT_WIDEP";   // 13.33 × 7.5 in (same as WIDE)

// Custom layout
pres.defineLayout({ name: "CUSTOM_16_9", width: 10, height: 5.625 });
pres.layout = "CUSTOM_16_9";
```

### Coordinates Are in Inches (Not EMU)

All `x`, `y`, `w`, `h` values are **inches** as JavaScript numbers.
This differs from raw OOXML (EMU) and python-pptx (`Inches()`).

### Slide Masters — Define Before Slides

```javascript
pres.defineSlideMaster({
  title: "MASTER_DARK",
  background: { color: "1F2D3D" },
  objects: [
    // Logo image in every slide
    { image: { path: "logo.png", x: 11.5, y: 6.8, w: 1.5, h: 0.5 } },
    // Footer text
    { text: { text: "Confidential", options: {
        x: 0.3, y: 7.0, w: 5, h: 0.3, color: "AAAAAA", fontSize: 9
    }}},
  ],
  slideNumber: { x: 12.5, y: 7.0, color: "AAAAAA", fontSize: 9 },
});
```

Slides referencing `masterName: "MASTER_DARK"` inherit the background and objects.

### Tables — Always Specify Column Widths

```javascript
const rows = [
  [{ text: "Header 1", options: { bold: true } }, "Header 2", "Header 3"],
  ["Row 1 A", "Row 1 B", "Row 1 C"],
  ["Row 2 A", "Row 2 B", "Row 2 C"],
];

slide.addTable(rows, {
  x: 0.5, y: 1.5,
  w: 12.33,
  colW: [4.0, 4.0, 4.33],   // must sum to w
  border: { pt: 1, color: "CCCCCC" },
  fill: { color: "F5F5F5" },
  color: "333333",
  fontSize: 12,
  rowH: 0.5,
});
```

Column widths in `colW` must sum to `w`. Header row styling via cell-level options.

### Charts — Built-in Types

```javascript
const chartData = [
  {
    name: "Revenue",
    labels: ["Q1", "Q2", "Q3", "Q4"],
    values: [120000, 145000, 138000, 210000],
  },
  {
    name: "Expenses",
    labels: ["Q1", "Q2", "Q3", "Q4"],
    values: [90000, 105000, 98000, 155000],
  },
];

slide.addChart(pres.ChartType.bar, chartData, {
  x: 0.5, y: 1.2, w: 12.33, h: 5.0,
  barGrouping: "clustered",
  showLegend: true, legendPos: "b",
  showValue: true,
  chartColors: ["0070C0", "FF0000"],
  valAxisTitle: "USD",
  catAxisTitle: "Quarter",
});
```

Available chart types: `bar`, `line`, `pie`, `doughnut`, `area`, `scatter`, `bubble`, `radar`.

### Images

```javascript
// From file path
slide.addImage({ path: "photo.jpg", x: 0.5, y: 1.0, w: 4.0, h: 3.0, altText: "Photo" });

// From base64 data
const imgData = fs.readFileSync("logo.png").toString("base64");
slide.addImage({ data: `image/png;base64,${imgData}`, x: 0.0, y: 0.0, w: 2.0, h: 1.0 });
```

### Text — Avoid Raw `\n`

Use an array of text objects with `breakLine`:

```javascript
// WRONG — \n inside text does not create a paragraph break
slide.addText("Line one\nLine two", { x: 0.5, y: 1.0, w: 9, h: 2 });

// CORRECT — array of text objects
slide.addText(
  [
    { text: "Line one", options: { fontSize: 18 } },
    { text: "\n" },
    { text: "Line two", options: { fontSize: 18 } },
  ],
  { x: 0.5, y: 1.0, w: 9, h: 2 }
);
```

### Bullet Lists

```javascript
slide.addText(
  [
    { text: "First point",  options: { bullet: true, indentLevel: 0 } },
    { text: "Sub-point",    options: { bullet: true, indentLevel: 1 } },
    { text: "Second point", options: { bullet: true, indentLevel: 0 } },
  ],
  { x: 0.5, y: 1.5, w: 12.0, h: 4.0, fontSize: 18, color: "333333" }
);
```

### Speaker Notes

```javascript
slide.addNotes("Key takeaway: revenue grew 15% YoY.\nSee appendix for methodology.");
```

### Shapes

```javascript
slide.addShape(pres.ShapeType.roundRect, {
  x: 1.0, y: 1.0, w: 4.0, h: 2.0,
  fill: { color: "0070C0" },
  line: { color: "004080", width: 2 },
});
```

---

## Typical Deck Structure

```javascript
const pptxgen = require("pptxgenjs");
const fs = require("fs");

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "AI Agent";
pres.title = "Q3 Report";

// Title slide
const titleSlide = pres.addSlide();
titleSlide.addText("Q3 2026 Results", {
  x: 0.5, y: 2.5, w: 12.33, h: 1.5,
  fontSize: 44, bold: true, color: "003366", align: "center",
});
titleSlide.addText("Prepared by Finance Team", {
  x: 0.5, y: 4.2, w: 12.33, h: 0.8,
  fontSize: 20, color: "666666", align: "center",
});

// Content slide
const slide2 = pres.addSlide();
slide2.addText("Revenue Overview", { x: 0.5, y: 0.3, w: 12, h: 0.8, fontSize: 28, bold: true });
slide2.addChart(pres.ChartType.bar, chartData, { x: 0.5, y: 1.2, w: 12.33, h: 5.5 });
slide2.addNotes("Discuss Q3 numbers and outlook.");

pres.writeFile({ fileName: "q3-report.pptx" });
```

---

## Post-Generation Validation

Always run after generating:

```bash
python scripts/validate.py output.pptx
```

For visual QA:

```bash
python scripts/thumbnails.py output.pptx -o output-slides/
```

Inspect each PNG before delivery. If validation fails: unpack with
`safe-unpack.py`, locate invalid XML, fix, repack with `pack.py`.
