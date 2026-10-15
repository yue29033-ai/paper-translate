"""Reflow reviewed translations and reuse original PDF regions without rasterizing."""
from __future__ import annotations

import argparse
import copy
import io
import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter, PageObject, Transformation
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, RectangleObject, DecodedStreamObject, NumberObject
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

from common import load_manifest, write_json, translation_paragraph_markup
from extract import normalized_reader


def choose_font(explicit=None):
    candidates = [explicit] if explicit else [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/usr/share/fonts/truetype/arphic/uming.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf',
    ]
    failures = []
    for p in candidates:
        if p and Path(p).is_file():
            try:
                pdfmetrics.registerFont(TTFont('PaperChinese', p))
                return str(Path(p).resolve())
            except Exception as e:
                failures.append(f'{p}: {e}')
    raise ValueError('No usable embeddable CJK TrueType font. Pass --font PATH. ' + '; '.join(failures))


def clipped_page(page, bbox):
    """Copy a top-left source region, retaining vector/image content and URI links."""
    w, h = float(page.mediabox.width), float(page.mediabox.height)
    x0, y0, x1, y1 = bbox
    clipped = copy.copy(page)
    clipped.indirect_reference = None
    clipped.cropbox = RectangleObject([x0, h - y1, x1, h - y0])
    clipped.pop(NameObject('/Annots'), None)
    return clipped


def merge_region(target, source, bbox, dest):
    """Place bbox in dest=(left,bottom,width,height), preserving aspect ratio."""
    x0, y0, x1, y1 = bbox
    dx, dy, dw, dh = dest
    sh = float(source.mediabox.height)
    scale = dw / (x1 - x0)
    if abs(dh - (y1 - y0) * scale) > .05:
        raise ValueError('Region placement would distort aspect ratio.')
    tx, ty = dx - x0 * scale, dy - (sh - y1) * scale
    # Keep original content-stream bytes inside a Form XObject. Parsing and
    # reserializing the source stream would round its coordinates, changing
    # small glyph/vector edges on dense scientific pages.
    form = DecodedStreamObject()
    content = source.get_contents()
    form.set_data(content.get_data() if content is not None else b'')
    form.update({NameObject('/Type'): NameObject('/XObject'),
                 NameObject('/Subtype'): NameObject('/Form'),
                 NameObject('/FormType'): NumberObject(1),
                 NameObject('/BBox'): RectangleObject([x0, sh-y1, x1, sh-y0]),
                 NameObject('/Resources'): source.get('/Resources', DictionaryObject())})
    if source.get('/Group') is not None:
        form[NameObject('/Group')] = source['/Group']
    resources = target.get('/Resources')
    if resources is None:
        resources = DictionaryObject()
        target[NameObject('/Resources')] = resources
    resources = resources.get_object()
    objects = resources.get('/XObject')
    if objects is None:
        objects = DictionaryObject()
        resources[NameObject('/XObject')] = objects
    objects = objects.get_object()
    counter = len(objects)
    name = NameObject(f'/PreservedRegion{counter}')
    while name in objects:
        counter += 1
        name = NameObject(f'/PreservedRegion{counter}')
    objects[name] = form
    command = DecodedStreamObject()
    numbers = ' '.join(format(v, '.12g') for v in (scale, 0, 0, scale, tx, ty))
    command.set_data(f'q {numbers} cm {name} Do Q\n'.encode('ascii'))
    old_content = target.get_contents()
    joined = DecodedStreamObject()
    old_bytes = old_content.get_data() if old_content is not None else b''
    # A standalone source page starts with default graphics state. Isolate the
    # existing target stream so its last fill colour/text state cannot leak in.
    joined.set_data(b'q\n' + old_bytes + b'\nQ\n' + command.get_data())
    target[NameObject('/Contents')] = joined
    # Add only links actually contained in the region. pypdf merging a detached
    # page would otherwise copy unrelated hotspots without transforming them.
    for ref in source.get('/Annots', []):
        annot = ref.get_object()
        if annot.get('/Subtype') != '/Link' or '/Rect' not in annot:
            continue
        ax0, ay0, ax1, ay1 = map(float, annot['/Rect'])
        cx0, cy0 = max(ax0, x0), max(ay0, sh - y1)
        cx1, cy1 = min(ax1, x1), min(ay1, sh - y0)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        # URI actions survive moving pages; internal page destinations do not
        # have a stable target after Chinese continuation pages are inserted.
        action = annot.get('/A')
        action = action.get_object() if action is not None else None
        if not action or action.get('/S') != '/URI':
            continue
        new = DictionaryObject({
            NameObject('/Type'): NameObject('/Annot'),
            NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): RectangleObject([cx0*scale+tx, cy0*scale+ty, cx1*scale+tx, cy1*scale+ty]),
            NameObject('/Border'): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(0)]),
            NameObject('/A'): action,
        })
        target.setdefault(NameObject('/Annots'), ArrayObject()).append(new)


def styles(font_size):
    base = dict(fontName='PaperChinese', wordWrap='CJK', splitLongWords=1,
                allowWidows=0, allowOrphans=0, spaceBefore=0, spaceAfter=0)
    return {
        'body': ParagraphStyle('Body', fontSize=font_size, leading=font_size*1.65, **base),
        'abstract': ParagraphStyle('Abstract', fontSize=font_size, leading=font_size*1.65, **base),
        'keywords': ParagraphStyle('Keywords', fontSize=font_size, leading=font_size*1.6, **base),
        'caption': ParagraphStyle('Caption', fontSize=font_size, leading=font_size*1.6, **base),
        'heading': ParagraphStyle('Heading', fontSize=font_size+1, leading=(font_size+1)*1.6,
                                  textColor='#225952', **base),
        'title': ParagraphStyle('Title', fontSize=font_size+5, leading=(font_size+5)*1.5, **base),
    }


def compose(manifest_path, out_dir, stem, font=None, font_size=10.5, draft=False):
    m = load_manifest(manifest_path)
    if not re.fullmatch(r'[\w\u4e00-\u9fff.() -]+', stem) or stem in ('.', '..'):
        raise ValueError('Use a simple filename stem without path separators.')
    unfinished = [b['id'] for p in m['pages'] for b in p['blocks']
                  if b['action'] == 'translate' and not str(b.get('translation') or '').strip()]
    unreviewed = [p['number'] for p in m['pages'] if p.get('reviewed') is not True]
    if not draft and (unfinished or unreviewed):
        raise ValueError(f'Incomplete translations {unfinished[:12]} or unreviewed layout pages {unreviewed}. Use --draft for a marked preview.')
    if not 9 <= font_size <= 18:
        raise ValueError('--font-size must be between 9 and 18 pt; use continuation pages instead of tiny text.')
    selected = [p['number'] for p in m['pages']]
    coverage = 'full' if selected == list(range(1, m['source']['page_count']+1)) else 'sample'
    suffix = '_草稿' if draft else ('_样例' if coverage == 'sample' else '')
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        'chinese': out_dir / f'{stem}{suffix}_中文.pdf',
        'bilingual': out_dir / f'{stem}{suffix}_中英对照.pdf',
        'layout': out_dir / f'{stem}{suffix}.layout.json',
    }
    protected = Path(m['source']['path']).resolve()
    if protected in paths.values() or Path(manifest_path).resolve() in paths.values():
        raise ValueError('Output must not overwrite source PDF or manifest.')
    existing = [str(p) for p in paths.values() if p.exists()]
    if existing:
        raise ValueError('Output already exists. Choose a new --stem: ' + ', '.join(existing))
    font_path = choose_font(font)
    glyphs = pdfmetrics.getFont('PaperChinese').face.charToGlyph
    chars = set('译文原文第页续样例草稿')
    for p in m['pages']:
        for b in p['blocks']:
            if b['action'] == 'translate':
                chars.update(str(b.get('translation') or ''))
    missing = sorted(c for c in chars if not c.isspace() and ord(c) not in glyphs)
    if missing:
        raise ValueError('Chosen font lacks glyphs: ' + ''.join(missing[:60]) + '. Pass another --font.')
    source, geometry = normalized_reader(m['source']['path'])
    if len(source.pages) != m['source']['page_count']:
        raise ValueError('Source page count no longer matches manifest.')
    st = styles(font_size)
    log = dict(schema_version=1, manifest=str(Path(manifest_path).resolve()),
               source_sha256=m['source']['sha256'], coverage=coverage, selected_pages=selected,
               draft=draft, font=font_path, font_size=font_size,
               outputs={k:str(v) for k,v in paths.items() if k != 'layout'},
               pages=[], placements=[], source_geometry=geometry)
    chinese, bilingual = PdfWriter(), PdfWriter()
    buffers = []
    nout = 0
    for group in m['pages']:
        source_page = source.pages[group['number']-1]
        w, h = group['width'], group['height']
        if abs(float(source_page.mediabox.width)-w) > .1 or abs(float(source_page.mediabox.height)-h) > .1:
            raise ValueError(f'Geometry changed on source page {group["number"]}; re-extract.')
        margin, top, bottom = min(42., w*.075), 48., 40.
        content_w = w-2*margin
        if content_w < 100 or h-top-bottom < 100:
            raise ValueError('Visible page too small for readable reflow.')
        continuation = 0
        canv = None
        buf = None
        regions = []
        y = 0

        def start_page():
            nonlocal canv, buf, regions, y, continuation, nout
            continuation += 1
            nout += 1
            buf = io.BytesIO()
            canv = canvas.Canvas(buf, pagesize=(w,h))
            canv.setTitle(f'{stem} | 原文第 {group["number"]} 页')
            canv.setFont('PaperChinese', 8)
            canv.setFillColorRGB(.35,.4,.4)
            tag = ' · 草稿' if draft else (' · 样例' if coverage == 'sample' else '')
            continued = f' · 续页 {continuation-1}' if continuation > 1 else ''
            canv.drawString(margin, h-25, f'译文 · 原文第 {group["number"]} 页{continued}{tag}')
            canv.drawRightString(w-margin, 20, f'{nout}')
            canv.setStrokeColorRGB(.8,.85,.85)
            canv.line(margin,h-33,w-margin,h-33)
            regions = []
            y = h-top
            log['pages'].append(dict(output_page=nout, source_page=group['number'],
                                     continuation=continuation, width=w, height=h))

        def end_page():
            canv.save()
            buf.seek(0)
            buffers.append(buf)
            page = PdfReader(buf).pages[0]
            for box, dest in regions:
                merge_region(page, source_page, box, dest)
            chinese.add_page(page)
            pair = PageObject.create_blank_page(width=2*w, height=h)
            # Preserve transformed URI hotspots along with the original content.
            merge_region(pair, page, [0,0,w,h], [0,0,w,h])
            merge_region(pair, source_page, [0,0,w,h], [w,0,w,h])
            bilingual.add_page(pair)

        start_page()
        for b in group['blocks']:
            if b['action'] == 'omit':
                continue
            if b['action'] == 'preserve':
                x0,y0,x1,y1 = b['bbox']
                bw,bh = x1-x0,y1-y0
                scale = min(1., content_w/bw, (h-top-bottom)/bh)
                dw,dh = bw*scale,bh*scale
                if dh > y-bottom+.01:
                    end_page(); start_page()
                left = margin+(content_w-dw)/2
                dest = [left,y-dh,dw,dh]
                regions.append((b['bbox'],dest))
                log['placements'].append(dict(id=b['id'], action='preserve',
                    source_page=group['number'], output_page=nout,
                    source_bbox=b['bbox'], bbox=[left,h-y,left+dw,h-y+dh], scale=scale))
                y -= dh+10
                continue
            text = str(b.get('translation') or '').strip()
            if not text:
                text = '[未翻译] ' + b.get('text','')
            html = translation_paragraph_markup(b, text)
            paragraph = Paragraph(html, st.get(b.get('role'), st['body']))
            pending = [paragraph]
            fragment = 0
            while pending:
                para = pending.pop(0)
                pw,ph = para.wrap(content_w, h-top-bottom)
                available = y-bottom
                # Keep short headings and the start of their following text together.
                extra = st['body'].leading if b.get('role') in ('heading','title') else 0
                if ph+extra > available and ph+extra <= h-top-bottom:
                    end_page(); start_page(); available=y-bottom
                if ph > available+.01:
                    parts = para.split(content_w, available)
                    if not parts:
                        if abs(y-(h-top)) < .01:
                            raise ValueError(f'{b["id"]}: paragraph cannot fit/split at the chosen font.')
                        end_page(); start_page(); pending.insert(0,para); continue
                    head,tail = parts[0],parts[1:]
                    _,ph = head.wrap(content_w,available)
                    pending = tail+pending
                else:
                    head = para
                fragment += 1
                head.drawOn(canv,margin,y-ph)
                log['placements'].append(dict(id=b['id'], action='translate',
                    source_page=group['number'], output_page=nout,
                    fragment=fragment, bbox=[margin,h-y,margin+content_w,h-y+ph]))
                y -= ph+9
                if pending:
                    end_page(); start_page()
        end_page()
    chinese.add_metadata({'/Title': f'{stem} 中文译文', '/Subject': f'{coverage}; AI translation; source page mapping in layout JSON'})
    bilingual.add_metadata({'/Title': f'{stem} 中英对照', '/Subject': f'{coverage}; Chinese left, original right'})
    for key,writer in [('chinese',chinese),('bilingual',bilingual)]:
        with paths[key].open('wb') as f:
            writer.write(f)
    write_json(paths['layout'],log)
    return paths,log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--stem', default='论文译文')
    parser.add_argument('--font')
    parser.add_argument('--font-size', type=float, default=10.5)
    parser.add_argument('--draft', action='store_true')
    args = parser.parse_args()
    try:
        paths,log = compose(args.manifest,args.out_dir,args.stem,args.font,args.font_size,args.draft)
        print(json.dumps(dict(outputs={k:str(v) for k,v in paths.items()},
                             pages=len(log['pages']), coverage=log['coverage']),ensure_ascii=False))
    except (ValueError,KeyError,FileNotFoundError) as e:
        parser.exit(2, f'compose: {e}\n')


if __name__ == '__main__':
    main()
