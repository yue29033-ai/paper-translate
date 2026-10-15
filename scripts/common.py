"""Shared manifest validation. No network, credentials, or implicit writes."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape


INLINE_SUBSCRIPTS = {'k_cat': ('k', 'cat'), 'k_uncat': ('k', 'uncat'),
                     'K_M': ('K', 'M'), 'K_I': ('K', 'I'), 'pK_a': ('pK', 'a'),
                     'pK_a1': ('pK', 'a1'), 'pK_a2': ('pK', 'a2'),
                     'V_max': ('V', 'max'), 'k_obs': ('k', 'obs'),
                     'v_0': ('v', '0'), '[E]_0': ('[E]', '0'), 'T_m': ('T', 'm')}
INLINE_PATTERN = re.compile(r'(?<![A-Za-z0-9_])(?:k_uncat|k_cat|k_obs|K_[MI]|V_max|v_0|\[E\]_0|pK_a[12]?|T_m)(?![A-Za-z0-9_])')


def translation_visible_text(block):
    text = str(block.get('translation') or '')
    if block.get('inline_math') is True:
        text = INLINE_PATTERN.sub(lambda m: ''.join(INLINE_SUBSCRIPTS[m.group()]), text)
    return text


def translation_paragraph_markup(block, fallback=''):
    text = str(block.get('translation') or fallback).strip()
    escaped = escape(text).replace('\n', '<br/>')
    if block.get('inline_math') is True:
        escaped = INLINE_PATTERN.sub(lambda m: INLINE_SUBSCRIPTS[m.group()][0] +
            '<sub>' + INLINE_SUBSCRIPTS[m.group()][1] + '</sub>', escaped)
    return escaped


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_manifest(path):
    m = json.loads(Path(path).read_text(encoding='utf-8'))
    if m.get('schema_version') != 1:
        raise ValueError('Unsupported manifest schema_version; expected 1.')
    src = m['source']
    source = Path(src['path']).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f'Source PDF not found: {source}')
    if file_hash(source) != src['sha256']:
        raise ValueError('Source PDF hash changed. Re-extract before composing or verifying.')
    if not m.get('pages'):
        raise ValueError('No selected source pages.')
    ids, pages = set(), set()
    for p in m['pages']:
        n = p['number']
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= src['page_count'] or n in pages:
            raise ValueError(f'Invalid or repeated source page: {n}')
        pages.add(n)
        w, h = float(p['width']), float(p['height'])
        if not (math.isfinite(w) and math.isfinite(h) and w > 0 and h > 0):
            raise ValueError(f'Invalid dimensions on page {n}')
        if not p.get('blocks'):
            raise ValueError(f'Page {n} has no blocks. Preserve a genuinely blank page explicitly.')
        for b in p['blocks']:
            bid = b['id']
            if not isinstance(bid, str) or not bid or bid in ids:
                raise ValueError(f'Invalid or duplicate block ID: {bid}')
            ids.add(bid)
            if b.get('action') not in ('translate', 'preserve', 'omit'):
                raise ValueError(f'{bid}: invalid action')
            box = b.get('bbox', [])
            if len(box) != 4 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in box):
                raise ValueError(f'{bid}: bbox must contain four finite coordinates')
            x0, y0, x1, y1 = box
            if not (-.1 <= x0 < x1 <= w + .1 and -.1 <= y0 < y1 <= h + .1):
                raise ValueError(f'{bid}: bbox outside visible page')
            if b['action'] == 'translate' and not isinstance(b.get('text'), str):
                raise ValueError(f'{bid}: translate block requires source text')
            if b['action'] == 'omit' and not str(b.get('reason', '')).strip():
                raise ValueError(f'{bid}: omit requires an explicit reason')
    return m
