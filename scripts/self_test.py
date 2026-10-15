#!/usr/bin/env python3
"""Offline synthetic integration checks; no paper translation or retained outputs.

Run with the skill's Python runtime: self_test.py [--font /path/to/CJK.ttf].
The fixtures contain invented text and deliberately expanded Chinese text to
exercise continuation pages, not to evaluate semantic translation quality.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from common import file_hash, write_json
from compose import choose_font, compose
from extract import normalized_reader, reader_bytes
from verify import compare_images, render_box, verify


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixture(path):
    image = Image.new('RGB', (100, 70), '#eff9fa')
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 90, 60), fill='#218c74')
    draw.ellipse((25, 15, 65, 55), fill='#ffcc66')
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(400, 500))
    pdf.drawImage(ImageReader(image), 50, 320, width=100, height=70)
    pdf.setStrokeColorRGB(.1, .3, .7)
    pdf.setLineWidth(1.5)
    pdf.line(180, 320, 340, 320)
    pdf.line(180, 320, 180, 425)
    pdf.line(180, 325, 230, 345)
    pdf.line(230, 345, 310, 405)
    pdf.setFont('Helvetica', 12)
    pdf.drawString(50, 440, 'A  raster panel + vector labels')
    pdf.drawString(185, 430, 'B  Vector chart')
    pdf.drawString(190, 305, 'Time (h)')
    pdf.linkURL('https://example.org/figure', (50, 435, 240, 450), relative=0)
    pdf.setFillColorRGB(0, 0, 0)
    pdf.drawString(35, 260, 'Synthetic body: value 12.3 mg; n = 7.')
    pdf.showPage()
    pdf.setFont('Helvetica', 12)
    pdf.drawString(35, 420, 'SECOND SOURCE PAGE, independently anchored.')
    pdf.save()
    path.write_bytes(buffer.getvalue())


def manifest(source):
    translated = ('这是一段合成中文，用于检验长段落在续页后仍然完整可搜索。'
                  '原始图像、矢量标记和坐标应当保持清晰，数字12.3与单位mg保留。') * 32
    block = lambda bid, role, action, bbox, text='', translation=None: dict(
        id=bid, role=role, action=action, bbox=bbox, text=text, translation=translation)
    return dict(schema_version=1,
                source=dict(path=str(source), sha256=file_hash(source), page_count=2),
                glossary=[], review=dict(translation_checked=True, visual_checked=True, notes=['Synthetic fixture only.']),
                pages=[dict(number=1, width=400, height=500, reviewed=True, blocks=[
                    block('figure', 'figure', 'preserve', [40, 45, 350, 205]),
                    block('body', 'body', 'translate', [30, 225, 375, 250],
                          'Synthetic body: value 12.3 mg; n = 7.', translated)]),
                       dict(number=2, width=400, height=500, reviewed=True, blocks=[
                    block('second', 'body', 'translate', [30, 65, 390, 95],
                          'SECOND SOURCE PAGE, independently anchored.', '第二原文页有独立的对应关系。')])])


def checked_report(manifest_path, layout_path, path, expected_ready=True):
    _, report = verify(manifest_path, layout_path, report_path=path)
    failed = [item['name'] for item in report['machine_checks'] if not item['passed']]
    require(report['ready'] is expected_ready, f'Unexpected ready={report["ready"]}; failed={failed}')
    return report, failed


def rotation_checks(source, directory):
    original = PdfReader(io.BytesIO(source.read_bytes()))
    writer = PdfWriter()
    for rotation in (0, 90, 180, 270):
        page = writer.add_page(original.pages[0])
        page.cropbox = RectangleObject([20, 30, 380, 470])
        page.rotate(rotation)
    path = directory / 'rotated.pdf'
    with path.open('wb') as stream:
        writer.write(stream)
    before = file_hash(path)
    normalized, notes = normalized_reader(path)
    a = pdfium.PdfDocument(path.read_bytes())
    b = pdfium.PdfDocument(reader_bytes(normalized))
    try:
        for i, rotation in enumerate((0, 90, 180, 270)):
            result = compare_images(render_box(a[i]), render_box(b[i]))
            require(result['passed'], f'CropBox/rotation {rotation} changed rendered content: {result}')
            require(notes[i]['transformed_link_count'] == 1, 'URI Link lost during normalization')
            require(normalized.pages[i].get('/Rotate', 0) == 0, 'Page rotation was not normalized')
        require(file_hash(path) == before, 'Normalization modified its source file')
    finally:
        a.close()
        b.close()
    return 4


def run(font=None):
    font_path = choose_font(font)
    checks = 0
    with tempfile.TemporaryDirectory(prefix='paper-translate-self-test-') as temp:
        directory = Path(temp)
        source = directory / 'source.pdf'
        fixture(source)
        source_hash = file_hash(source)
        checks += rotation_checks(source, directory)
        data = manifest(source)
        mp = directory / 'manifest.json'
        write_json(mp, data)
        paths, layout = compose(mp, directory / 'out', '合成测试', font=font_path)
        first = [p for p in layout['pages'] if p['source_page'] == 1]
        require(len(first) > 1, 'Long Chinese paragraph did not produce continuation pages')
        require([p['continuation'] for p in first] == list(range(1, len(first) + 1)), 'Broken continuation numbering')
        report, _ = checked_report(mp, paths['layout'], directory / 'positive.qa.json')
        require(report['completed_paper'], 'Complete synthetic scope was not recognized')
        required = {'preserved_regions_render_match_original', 'bilingual_left_and_right_render_match',
                    'searchable_translation_text', 'source_character_and_raster_coverage'}
        require(required <= {c['name'] for c in report['machine_checks'] if c['passed']}, 'Primary visible checks missing')
        checks += 2  # Main region preservation + continuation/left-right anchoring.

        unreviewed = copy.deepcopy(data)
        unreviewed['pages'][0]['reviewed'] = False
        bad_manifest = directory / 'unreviewed.json'
        write_json(bad_manifest, unreviewed)
        _, failed = checked_report(bad_manifest, paths['layout'], directory / 'unreviewed.qa.json', False)
        require('reviewed_source_regions' in failed, 'Unreviewed source regions were accepted')
        checks += 1

        uncovered = copy.deepcopy(data)
        uncovered['pages'][0]['blocks'][1]['bbox'] = [30, 255, 60, 265]
        bad_manifest = directory / 'uncovered.json'
        write_json(bad_manifest, uncovered)
        _, failed = checked_report(bad_manifest, paths['layout'], directory / 'uncovered.qa.json', False)
        require('source_character_and_raster_coverage' in failed, 'Missing source characters were accepted')
        checks += 1

        # Modify an actual output PDF, not only its layout or manifest.
        chinese = Path(paths['chinese'])
        good_bytes = chinese.read_bytes()
        reader = PdfReader(io.BytesIO(good_bytes))
        writer = PdfWriter()
        writer.add_page(PageObject.create_blank_page(width=400, height=500))
        for page in reader.pages[1:]:
            writer.add_page(page)
        try:
            with chinese.open('wb') as stream:
                writer.write(stream)
            _, failed = checked_report(mp, paths['layout'], directory / 'tampered.qa.json', False)
            require('preserved_regions_render_match_original' in failed, 'Tampered figure region was accepted')
            require('bilingual_left_and_right_render_match' in failed, 'Changed Chinese output was accepted as matching the bilingual left')
        finally:
            chinese.write_bytes(good_bytes)
        checks += 1
        require(file_hash(source) == source_hash, 'Source PDF changed during the pipeline')
        checks += 1
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', help='Embeddable CJK TrueType font; default uses compose.choose_font')
    args = parser.parse_args()
    print(json.dumps(dict(passed=run(args.font), failed=0, temporary_outputs_retained=False)))


if __name__ == '__main__':
    main()
