# ZoneTag

Teach a form once, then write those fields into PDF metadata for a folder of scans.

Page type comes from identifier rules on the page, so page order does not matter. A page that matches no rule is skipped. Copies are written to an output folder. The original files stay where they are.

Each tag is written to the PDF Info dictionary, which Acrobat shows under Document Properties, Custom, and to XMP in the `pdfx` namespace.

## Run

```bat
run.bat
```

Or, from this folder:

```bat
.venv\Scripts\python.exe -m zonetag
```

The window layout is `ui/mainwindow.ui`. Open that file in Qt Designer to move the docks. Behavior lives in `zonetag/window.py`.

## Workflow

1. Open one sample PDF.
2. Add a page type for each kind of page in the form.
3. Set the box role to Identifier, draw a box around stable wording, click Read box, then Use as expected. Use Whole page when the phrase can sit anywhere on that page.
4. Set the box role to Value, type a tag name such as `ContractNumber`, and draw the box around the value.
5. Test this file. Matched pages, skipped pages, and the tags are listed at the bottom.
6. Save the template.
7. Choose a folder. The batch list fills on its own.
8. In Conflicts, keep or overwrite any tag that already has a different value. Apply writes the copies.

A tag that is missing is written. A tag that already holds the same text is left as-is and noted. Signed and encrypted PDFs are skipped.

Value pad, in the inspector, grows every value box by that percent of the page so a slightly shifted scan still covers the field.
