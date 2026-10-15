# Manifest contract (schema_version: 1)

`extract.py` writes a proposed layout, not a finished translation. All bounding boxes use PDF points with origin at the top left of the visible page, in `[x0, top, x1, bottom]` order. Page numbers are one based. `pages` contains selected source pages in output order. IDs must be unique across the document.

```json
{
  "schema_version": 1,
  "source": {"path": "/absolute/original.pdf", "sha256": "...", "page_count": 17},
  "glossary": [{"en": "drought resistance", "zh": "抗旱性"}],
  "review": {"translation_checked": false, "visual_checked": false, "notes": []},
  "pages": [{
    "number": 3, "width": 612, "height": 783, "reviewed": false,
    "blocks": [
      {"id": "p0003-b0001", "role": "figure", "action": "preserve",
       "bbox": [60, 60, 550, 400], "text": "", "translation": null},
      {"id": "p0003-b0002", "role": "caption", "action": "translate",
       "bbox": [60, 410, 550, 470], "text": "Figure 1. ...", "translation": null}
    ]
  }]
}
```

- `action=translate`: render the complete `translation` as searchable Chinese text. Roles `title`, `heading`, `caption`, `body`, `abstract`, `keywords` select typography; other roles use body typography. `translation` is plain text; XML-like characters are escaped by the compositor.
- Optional `inline_math: true` on a translate block typesets the exact tokens `k_cat`, `k_uncat`, `k_obs`, `K_M`, `K_I`, `V_max`, `v_0`, `[E]_0`, `pK_a`, `pK_a1`, `pK_a2`, `T_m` using their scientific subscripts. Other identifiers and underscores are unchanged. Verification compares the corresponding visible text, rather than expecting the literal underscore in the PDF. Use source PDF regions for complex equations.
- `action=preserve`: copy the entire source PDF region, retaining image objects, vector graphics and labels. The region is an indivisible unit; its `text` is only an audit aid. This applies to figures, tables, equations, author information and references. Place it in reading order among text blocks. Clip no panel letters, scales, footnotes, legends or equation numbers.
- `action=omit`: omit a confirmed running header/footer or redundant ornament. Include a nonempty `reason`. Never use this action to bypass untranslated content.
- Set `page.reviewed=true` only after checking the proposed regions and reading order against a rendered source page. Automatic raster-image proposals cannot identify every vector plot, table or formula. Replace/split/reorder/add regions as required before translation.
- `review.translation_checked` and `review.visual_checked` are human/agent review attestations, not machine-detected facts. Set them only after the corresponding review; machine checks do not set them.
- Optional `join_next: "id"` flags a sentence continued in another block/page. Translate joined text with context, distribute the complete translation across its source anchors once, then remove the flag. Unresolved joins are reported by verification.
- Optional `warnings: ["..."]` on a page records unresolved extraction/OCR/layout concerns. Clear only after resolution; unresolved warnings prevent a ready result.

The compositor records every text/region placement in `<stem>.layout.json`, including source page, output page, box and scale. Both PDFs have the same translated-page sequence. Each bilingual page repeats the complete matching source page on the right, including when a Chinese source-page group continues. `<stem>.qa.json` records what was actually checked and distinguishes sample output from complete-paper output.
