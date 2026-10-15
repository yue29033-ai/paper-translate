#!/usr/bin/env python3
"""Verify actual translated PDFs; review attestations never replace machine checks.

Offline dependencies: pypdf, pdfplumber, pypdfium2, Pillow and numpy.
Exit 0: ready for the stated scope; 1: incomplete review or failed checks;
2: could not execute validation. Original PDF is always opened read-only.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from common import file_hash, load_manifest, write_json, translation_visible_text
from extract import normalized_reader


SCALE = 2.0  # 144 dpi, identical for source and actual output comparisons.
# Fractional placement precision can alter a few antialiased edge pixels.
# These are strict ceilings, not similarity scores: all must pass.
DIFF_LIMITS = dict(changed_fraction=0.0002, severe_fraction=0.00002,
                   mean_absolute_error=0.02, channel_tolerance=8,
                   severe_channel_tolerance=32)


def compact(text):
    return re.sub(r'\s+', '', text or '')


def box_area(box):
    return max(0., box[2]-box[0]) * max(0., box[3]-box[1])


def contains(outer, inner, tolerance=0.15):
    return (outer[0]-tolerance <= inner[0] and outer[1]-tolerance <= inner[1]
            and inner[2] <= outer[2]+tolerance and inner[3] <= outer[3]+tolerance)


def render_box(page, box=None, scale=SCALE, pixel_size=None):
    """Render from the actual open PDF, with subpixel-exact visible-page crop.

    PDFium's WithMatrix applies after its CropBox/Rotate display transform.
    Unlike PdfPage.render(crop=...), this avoids integer-ceil crop shifts.
    """
    width, height = page.get_size()
    x0,y0,x1,y1 = box or [0.,0.,width,height]
    if not (0 <= x0 < x1 <= width+.15 and 0 <= y0 < y1 <= height+.15):
        raise ValueError(f'Render region outside actual PDF page: {box}, page {width} x {height}')
    fw,fh = (x1-x0)*scale,(y1-y0)*scale
    pw,ph = pixel_size or (max(1,math.ceil(fw)),max(1,math.ceil(fh)))
    if pw*ph > 100_000_000:
        raise ValueError('Render exceeds 100 million pixels; inspect exceptional page geometry.')
    bitmap = pdfium.PdfBitmap.new_native(pw,ph,pdfium.raw.FPDFBitmap_BGR)
    try:
        bitmap.fill_rect((255,255,255,255),0,0,pw,ph)
        matrix = pdfium.raw.FS_MATRIX(scale,0,0,scale,-x0*scale,-y0*scale)
        clip = pdfium.raw.FS_RECTF(0,0,fw,fh)
        pdfium.raw.FPDF_RenderPageBitmapWithMatrix(bitmap,page,ctypes.byref(matrix),
                                                  ctypes.byref(clip),pdfium.raw.FPDF_ANNOT)
        return bitmap.to_pil().convert('RGB').copy()
    finally:
        bitmap.close()


def compare_images(expected, actual):
    if expected.size != actual.size:
        return dict(passed=False, expected_size=list(expected.size), actual_size=list(actual.size),
                    reason='render_dimensions_differ')
    a,b = np.asarray(expected,dtype=np.int16),np.asarray(actual,dtype=np.int16)
    delta = np.abs(a-b)
    maximum = delta.max(axis=2)
    changed = int((maximum > DIFF_LIMITS['channel_tolerance']).sum())
    severe = int((maximum > DIFF_LIMITS['severe_channel_tolerance']).sum())
    pixels = int(maximum.size)
    mae = float(delta.mean())
    # Tiny images still tolerate at most two changed pixels, not an arbitrary
    # percentage. Report exact counts and thresholds for every comparison.
    changed_ceiling = max(2,math.floor(pixels*DIFF_LIMITS['changed_fraction']))
    severe_ceiling = max(0,math.floor(pixels*DIFF_LIMITS['severe_fraction']))
    passed = changed <= changed_ceiling and severe <= severe_ceiling and mae <= DIFF_LIMITS['mean_absolute_error']
    return dict(passed=bool(passed), pixels=pixels, changed_pixels=changed,
                changed_fraction=changed/pixels, severe_pixels=severe,
                severe_fraction=severe/pixels, mean_absolute_error=mae,
                max_channel_difference=int(maximum.max()),
                allowed_changed_pixels=changed_ceiling, allowed_severe_pixels=severe_ceiling)


def longest_prefix_present(expected, actual):
    lo,hi = 0,len(expected)
    while lo < hi:
        middle = (lo+hi+1)//2
        if expected[:middle] in actual:
            lo = middle
        else:
            hi = middle-1
    return lo


def contact_sheets(paths, directory, label):
    """Readable page inventory; full PNGs remain the inspection authority."""
    output=[]
    for start in range(0,len(paths),12):
        batch=paths[start:start+12]
        canvas=Image.new('RGB',(900,math.ceil(len(batch)/3)*400),'#e8ecec')
        draw=ImageDraw.Draw(canvas)
        for j,path in enumerate(batch):
            with Image.open(path) as original:
                thumb=original.convert('RGB')
                thumb.thumbnail((286,368))
            x=(j%3)*300+(300-thumb.width)//2
            y=(j//3)*400+23
            canvas.paste(thumb,(x,y))
            draw.text(((j%3)*300+10,(j//3)*400+5),f'{label} {start+j+1}',fill='black')
        path=directory/f'{label}-contact-{start//12+1:03d}.png'
        canvas.save(path)
        output.append(str(path))
    return output


def verify(manifest_path, layout_path, report_path=None, render_dir=None):
    manifest_path,layout_path=Path(manifest_path).resolve(),Path(layout_path).resolve()
    m=load_manifest(manifest_path)
    layout=json.loads(layout_path.read_text(encoding='utf-8'))
    if layout.get('schema_version') != 1:
        raise ValueError('Unsupported layout schema_version; expected 1.')
    source_path=Path(m['source']['path']).expanduser().resolve()
    selected=[p['number'] for p in m['pages']]
    expected_coverage='full' if selected == list(range(1,m['source']['page_count']+1)) else 'sample'
    report_path=Path(report_path).resolve() if report_path else layout_path.with_name(layout_path.name.removesuffix('.layout.json')+'.qa.json')
    if report_path in (manifest_path,layout_path,source_path):
        raise ValueError('QA report must not overwrite manifest, layout or source.')
    report=dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                manifest=str(manifest_path), manifest_sha256=file_hash(manifest_path),
                layout=str(layout_path), layout_sha256=file_hash(layout_path),
                source=dict(path=str(source_path),sha256=m['source']['sha256'],
                            expected_page_count=m['source']['page_count']),
                selected_pages=selected,coverage=expected_coverage,draft=layout.get('draft') is True,
                machine_checks=[],review=dict(translation_checked=m.get('review',{}).get('translation_checked') is True,
                                             visual_checked=m.get('review',{}).get('visual_checked') is True),
                machine_checks_passed=False,ready=False,completed_paper=False,
                limits={'render_scale':SCALE,'render_source':'original PDF with annotations',
                        'pixel_comparison':DIFF_LIMITS},
                limitations=['Character coverage checks bbox centres, not sentence meaning.',
                             'PDF text presence does not establish semantic translation accuracy.',
                             'Figure region bounds, reading order and all-page visual review require attestations.'])

    def check(name,passed,details=None):
        report['machine_checks'].append(dict(name=name,passed=bool(passed),details=details))

    check('layout_source_identity',layout.get('source_sha256')==m['source']['sha256'],
          {'layout_source_sha256':layout.get('source_sha256')})
    check('layout_scope',layout.get('selected_pages')==selected and layout.get('coverage')==expected_coverage,
          {'layout_selected_pages':layout.get('selected_pages'),'layout_coverage':layout.get('coverage')})
    unreviewed=[p['number'] for p in m['pages'] if p.get('reviewed') is not True]
    unfinished=[b['id'] for p in m['pages'] for b in p['blocks']
                if b['action']=='translate' and (not isinstance(b.get('translation'),str) or not b['translation'].strip())]
    joins=[b['id'] for p in m['pages'] for b in p['blocks'] if b.get('join_next')]
    warnings=[dict(page=p['number'],warnings=p.get('warnings')) for p in m['pages'] if p.get('warnings')]
    check('reviewed_source_regions',not unreviewed,{'unreviewed_pages':unreviewed})
    check('translation_fields_complete',not unfinished,{'missing_translation_blocks':unfinished})
    check('resolved_cross_page_joins',not joins,{'unresolved_blocks':joins})
    check('resolved_extraction_warnings',not warnings,{'unresolved':warnings})
    report['omitted_blocks']=[dict(id=b['id'],source_page=p['number'],reason=b['reason'])
                              for p in m['pages'] for b in p['blocks'] if b['action']=='omit']

    outputs={k:Path(layout['outputs'][k]).expanduser().resolve() for k in ('chinese','bilingual')}
    if report_path in outputs.values():
        raise ValueError('QA report must not overwrite an output PDF.')
    missing=[str(v) for v in outputs.values() if not v.is_file()]
    if missing:
        raise ValueError('Output PDFs not found: '+', '.join(missing))
    report['outputs']={k:dict(path=str(path),sha256=file_hash(path)) for k,path in outputs.items()}

    # Keep original_pdf distinct from normalized source: the original is the
    # rendering authority, so annotation/normalization losses remain visible.
    original_pdf=pdfium.PdfDocument(str(source_path))
    chinese_pdf=pdfium.PdfDocument(str(outputs['chinese']))
    bilingual_pdf=pdfium.PdfDocument(str(outputs['bilingual']))
    chinese_reader=PdfReader(str(outputs['chinese']),strict=False)
    normalized,geometry=normalized_reader(source_path)
    report['source']['actual_page_count']=len(original_pdf)
    check('actual_source_page_count',len(original_pdf)==m['source']['page_count']==len(normalized.pages))
    widgets=[]
    original_reader=PdfReader(str(source_path),strict=False)
    for number in selected:
        for reference in original_reader.pages[number-1].get('/Annots',[]):
            annotation=reference.get_object()
            if str(annotation.get('/Subtype',''))=='/Widget':
                widgets.append(dict(source_page=number,subtype='/Widget'))
    check('no_unverified_interactive_widgets',not widgets,
          {'widgets':widgets,'reason':'Matrix rendering checks annotation appearances, but interactive form widgets require a separate form-rendering path.'})
    writer=PdfWriter()
    for page in normalized.pages:
        writer.add_page(page)
    normalized_bytes=io.BytesIO()
    writer.write(normalized_bytes)
    normalized_bytes.seek(0)
    coverage=[]
    with pdfplumber.open(normalized_bytes) as document:
        for p in m['pages']:
            pg=document.pages[p['number']-1]
            available=[b for b in p['blocks'] if b['action'] in ('translate','preserve','omit')]
            chars=[c for c in pg.chars if str(c.get('text','')).strip()
                   and 0 <= (c['x0']+c['x1'])/2 <= p['width']
                   and 0 <= (c['top']+c['bottom'])/2 <= p['height']]
            missed=[]
            for c in chars:
                x,y=(c['x0']+c['x1'])/2,(c['top']+c['bottom'])/2
                if not any(b['bbox'][0]-.05 <= x <= b['bbox'][2]+.05 and
                           b['bbox'][1]-.05 <= y <= b['bbox'][3]+.05 for b in available):
                    missed.append(dict(text=c.get('text'),bbox=[c['x0'],c['top'],c['x1'],c['bottom']]))
            uncovered_images=[]
            images=[]
            for image in pg.images:
                box=[float(image[k]) for k in ('x0','top','x1','bottom')]
                # Ignore resources outside the visible crop, not clipped items.
                if box[2] <= 0 or box[3] <= 0 or box[0] >= p['width'] or box[1] >= p['height']:
                    continue
                visible=[max(0.,box[0]),max(0.,box[1]),min(p['width'],box[2]),min(p['height'],box[3])]
                covered=any(b['action'] in ('preserve','omit') and contains(b['bbox'],visible)
                            for b in available)
                images.append(dict(name=image.get('name'),bbox=visible,covered=covered))
                if not covered:
                    uncovered_images.append(dict(name=image.get('name'),bbox=visible))
            record=dict(source_page=p['number'],nonempty_characters=len(chars),
                        covered_characters=len(chars)-len(missed),
                        character_coverage=(len(chars)-len(missed))/len(chars) if chars else 1.,
                        missed_character_samples=missed[:40],missed_character_count=len(missed),
                        raster_images=images,uncovered_raster_images=uncovered_images)
            coverage.append(record)
    check('source_character_and_raster_coverage',all(not x['missed_character_count'] and
          not x['uncovered_raster_images'] for x in coverage),coverage)

    expected_blocks={b['id']:(p,b) for p in m['pages'] for b in p['blocks'] if b['action']!='omit'}
    placements=layout.get('placements',[])
    pages=layout.get('pages',[])
    maps={p.get('output_page'):p for p in pages}
    map_errors=[]
    if len(maps)!=len(pages) or sorted(maps)!=list(range(1,len(pages)+1)):
        map_errors.append('Output page map must be unique and contiguous from 1.')
    if not (len(pages)==len(chinese_pdf)==len(bilingual_pdf)==len(chinese_reader.pages)):
        map_errors.append('Actual output page counts differ from layout map.')
    expected_groups=[]
    for source in selected:
        group=[p for p in pages if p.get('source_page')==source]
        if not group or [p.get('continuation') for p in group]!=list(range(1,len(group)+1)):
            map_errors.append(f'Source page {source}: missing group or invalid continuation numbering.')
        expected_groups.extend(group)
    if expected_groups!=pages:
        map_errors.append('Source-page groups are missing, reordered or interleaved.')
    source_by_number={p['number']:p for p in m['pages']}
    for p in pages:
        n,source=p.get('output_page'),source_by_number.get(p.get('source_page'))
        if source is None or not isinstance(n,int) or not 1<=n<=min(len(chinese_pdf),len(bilingual_pdf)):
            map_errors.append(f'Invalid map entry: {p}')
            continue
        w,h=float(source['width']),float(source['height'])
        dims=[*chinese_pdf[n-1].get_size(),*bilingual_pdf[n-1].get_size()]
        wanted=[w,h,2*w,h]
        if any(abs(a-b)>.1 for a,b in zip(dims,wanted)) or abs(p.get('width',0)-w)>.1 or abs(p.get('height',0)-h)>.1:
            map_errors.append(f'Page {n}: actual/log dimensions {dims} != {wanted}.')
        sw,sh=original_pdf[source['number']-1].get_size()
        if abs(sw-w)>.1 or abs(sh-h)>.1:
            map_errors.append(f'Source page {source["number"]}: visible geometry differs from manifest.')
    check('actual_output_page_map_and_dimensions',not map_errors,{'errors':map_errors})

    by_block,by_page=defaultdict(list),defaultdict(list)
    placement_errors=[]
    for place in placements:
        bid=place.get('id')
        entry=expected_blocks.get(bid)
        page=maps.get(place.get('output_page'))
        box=place.get('bbox',[])
        if entry is None or page is None:
            placement_errors.append(f'Unknown block or output page: {place}')
            continue
        source,b=entry
        if place.get('source_page')!=source['number'] or page.get('source_page')!=source['number'] or place.get('action')!=b['action']:
            placement_errors.append(f'{bid}: action or source-page anchor differs.')
        if len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box) or box_area(box)<=0:
            placement_errors.append(f'{bid}: malformed placement bbox.')
            continue
        if not contains([0,0,page['width'],page['height']],box,.05):
            placement_errors.append(f'{bid}: placement outside output page.')
        if b['action']=='preserve':
            if place.get('source_bbox')!=b['bbox']:
                placement_errors.append(f'{bid}: source region changed since composition.')
            scale=place.get('scale',0)
            if not isinstance(scale,(int,float)) or not math.isfinite(scale) or scale<=0:
                placement_errors.append(f'{bid}: invalid preservation scale.')
            elif abs((box[2]-box[0])-(b['bbox'][2]-b['bbox'][0])*scale)>.05 or abs((box[3]-box[1])-(b['bbox'][3]-b['bbox'][1])*scale)>.05:
                placement_errors.append(f'{bid}: preservation aspect ratio/size changed.')
        by_block[bid].append(place)
        by_page[place['output_page']].append(place)
    for bid,(source,b) in expected_blocks.items():
        present=by_block.get(bid,[])
        if not present or b['action']=='preserve' and len(present)!=1:
            placement_errors.append(f'{bid}: missing block or repeated preserve region.')
        if b['action']=='translate' and present:
            if [p.get('fragment') for p in present]!=list(range(1,len(present)+1)):
                placement_errors.append(f'{bid}: invalid text fragment sequence.')
    observed_order=[]
    for place in placements:
        if not observed_order or observed_order[-1]!=place.get('id'):
            observed_order.append(place.get('id'))
    if observed_order!=list(expected_blocks):
        placement_errors.append('Block order differs from the current manifest; regenerate the PDFs.')
    if [p.get('output_page',0) for p in placements]!=sorted(p.get('output_page',0) for p in placements):
        placement_errors.append('Placement output pages are not in reading order.')
    for n,items in by_page.items():
        for i,a in enumerate(items):
            for b in items[i+1:]:
                box=[max(a['bbox'][0],b['bbox'][0]),max(a['bbox'][1],b['bbox'][1]),
                     min(a['bbox'][2],b['bbox'][2]),min(a['bbox'][3],b['bbox'][3])]
                if box_area(box)>.05:
                    placement_errors.append(f'Output page {n}: {a["id"]} overlaps {b["id"]}.')
    check('block_placements_and_nonoverlap',not placement_errors,{'errors':placement_errors})

    # pypdf's actual extracted output, not text copied from the layout log.
    page_text={i+1:compact(p.extract_text() or '') for i,p in enumerate(chinese_reader.pages)}
    text_results=[]
    for bid,(source,b) in expected_blocks.items():
        if b['action']!='translate':
            continue
        expected=compact(translation_visible_text(b))
        remaining=expected
        fragments=[]
        for place in by_block.get(bid,[]):
            actual=page_text.get(place['output_page'],'')
            count=longest_prefix_present(remaining,actual) if remaining else 0
            fragments.append(dict(output_page=place['output_page'],matched_characters=count))
            remaining=remaining[count:]
        text_results.append(dict(id=bid,passed=bool(expected) and not remaining,
                                 expected_characters=len(expected),missing_suffix_sample=remaining[:100],fragments=fragments))
    check('searchable_translation_text',all(x['passed'] for x in text_results),text_results)

    region_results=[]
    visual_map_results=[]
    render_paths={'chinese':[],'bilingual':[],'source':[]}
    render_directory=Path(render_dir).resolve() if render_dir else None
    if render_directory:
        render_directory.mkdir(parents=True,exist_ok=True)
    # Malformed placement data is already failed, and must not be used as a
    # guessed rendering transform. Other checks remain observable in the report.
    if not placement_errors and not map_errors:
        for bid,(source,b) in expected_blocks.items():
            if b['action']!='preserve':
                continue
            place=by_block[bid][0]
            target=render_box(chinese_pdf[place['output_page']-1],place['bbox'])
            expected=render_box(original_pdf[source['number']-1],b['bbox'],
                                SCALE*place['scale'],target.size)
            result=dict(id=bid,source_page=source['number'],output_page=place['output_page'],
                        source_bbox=b['bbox'],output_bbox=place['bbox'],scale=place['scale'],
                        comparison=compare_images(expected,target))
            region_results.append(result)
            if render_directory and not result['comparison']['passed']:
                safe_id=re.sub(r'[^A-Za-z0-9_-]','_',bid)[:60]+'-'+hashlib.sha256(bid.encode()).hexdigest()[:8]
                expected.save(render_directory/f'{safe_id}-expected.png')
                target.save(render_directory/f'{safe_id}-actual.png')
        for page in pages:
            n=page['output_page']; w,h=page['width'],page['height']
            translated=render_box(chinese_pdf[n-1])
            paired_left=render_box(bilingual_pdf[n-1],[0,0,w,h],pixel_size=translated.size)
            original=render_box(original_pdf[page['source_page']-1])
            paired_right=render_box(bilingual_pdf[n-1],[w,0,2*w,h],pixel_size=original.size)
            visual_map_results.append(dict(output_page=n,source_page=page['source_page'],
                left_matches_chinese=compare_images(translated,paired_left),
                right_matches_original=compare_images(original,paired_right)))
            if render_directory:
                chinese_path=render_directory/f'chinese-{n:04d}.png'
                bilingual_path=render_directory/f'bilingual-{n:04d}.png'
                translated.save(chinese_path)
                render_box(bilingual_pdf[n-1]).save(bilingual_path)
                render_paths['chinese'].append(chinese_path)
                render_paths['bilingual'].append(bilingual_path)
        if render_directory:
            for source_page in selected:
                path=render_directory/f'source-{source_page:04d}.png'
                render_box(original_pdf[source_page-1]).save(path)
                render_paths['source'].append(path)
    check('preserved_regions_render_match_original',not placement_errors and not map_errors and
          all(x['comparison']['passed'] for x in region_results),region_results)
    check('bilingual_left_and_right_render_match',not placement_errors and not map_errors and
          len(visual_map_results)==len(pages) and all(x['left_matches_chinese']['passed'] and
          x['right_matches_original']['passed'] for x in visual_map_results),visual_map_results)
    if render_directory:
        report['previews']={'directory':str(render_directory),
                            'pages':{key:[str(p) for p in paths] for key,paths in render_paths.items()},
                            'contact_sheets':[]}
        for key,paths in render_paths.items():
            report['previews']['contact_sheets'].extend(contact_sheets(paths,render_directory,key))

    report['machine_checks_passed']=all(c['passed'] for c in report['machine_checks'])
    report['ready']=(report['machine_checks_passed'] and all(report['review'].values()) and not report['draft'])
    report['completed_paper']=report['ready'] and expected_coverage=='full'
    report['pending_review']=[k for k,v in report['review'].items() if not v]
    write_json(report_path,report)
    original_pdf.close();chinese_pdf.close();bilingual_pdf.close()
    return report_path,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('layout')
    parser.add_argument('--report')
    parser.add_argument('--render-dir')
    args=parser.parse_args()
    try:
        path,report=verify(args.manifest,args.layout,args.report,args.render_dir)
        print(json.dumps(dict(report=str(path),ready=report['ready'],coverage=report['coverage'],
                              completed_paper=report['completed_paper'],
                              failed_checks=[x['name'] for x in report['machine_checks'] if not x['passed']],
                              pending_review=report['pending_review']),ensure_ascii=False))
        return 0 if report['ready'] else 1
    except Exception as error:
        print(f'verify: {type(error).__name__}: {error}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
