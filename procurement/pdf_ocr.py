"""Local per-page OCR. Original PDFs are never rewritten or sent to a model."""
import csv
import os
import re
import subprocess
import tempfile
from pathlib import Path

MAX_LINES = 1200
MAX_TEXT = 100000
MAX_OUTPUT = 8 * 1024 * 1024
MAX_PAGES = 12
MAX_REVIEW_LINES = MAX_LINES * MAX_PAGES


def _run(command, timeout):
    # The enclosing PDF worker owns this process group, including native children.
    env = {**os.environ, 'OMP_THREAD_LIMIT': '1', 'OMP_NUM_THREADS': '1'}
    try:
        result = subprocess.run(command, timeout=timeout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, check=False)
    except FileNotFoundError as exc:
        raise ValueError('Распознавание сканов недоступно: OCR не установлен') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Распознавание заняло слишком долго; выберите один лист меньшего размера') from exc
    if result.returncode:
        raise ValueError('Не удалось распознать скан; проверьте PDF или введите позиции вручную')


def parse_tsv(path):
    if path.stat().st_size > MAX_OUTPUT:
        raise ValueError('Слишком много текста на листе; выберите другой лист')
    groups = {}
    with path.open(encoding='utf-8', errors='strict', newline='') as stream:
        # Tesseract TSV has literal quote characters, not CSV quoting. A
        # drawing's standalone " must never swallow the following TSV rows.
        for row in csv.DictReader(stream, delimiter='\t', quoting=csv.QUOTE_NONE):
            word = (row.get('text') or '').strip()
            if not word or row.get('level') != '5':
                continue
            try:
                key = tuple(int(row[k]) for k in ('block_num', 'par_num', 'line_num'))
                confidence = float(row['conf'])
                left, top, height = (int(row[k]) for k in ('left', 'top', 'height'))
                width = int(row.get('width') or 0)
                if min(left, top, width, height) < 0 or not 0 <= confidence <= 100:
                    raise ValueError
            except (KeyError, ValueError) as exc:
                raise ValueError('OCR вернул некорректные данные; заявка не создана') from exc
            groups.setdefault(key, []).append((left, top, height, word, confidence, width))
            if len(groups) > MAX_LINES:
                raise ValueError('Слишком много строк на листе; выберите другой лист')
    lines = []
    for words in sorted(groups.values(), key=lambda w: (min(x[1] for x in w), min(x[0] for x in w))):
        words.sort(key=lambda w: w[0])
        text = ' '.join(w[3] for w in words)
        lines.append({'line': len(lines) + 1, 'text': text,
                      'confidence': round(min(w[4] for w in words) / 100, 3)})
        if all(w[5] > 0 for w in words):
            x, y = min(w[0] for w in words), min(w[1] for w in words)
            lines[-1]['bbox'] = {'left': x, 'top': y,
                'width': max(w[0]+w[5] for w in words)-x,
                'height': max(w[1]+w[2] for w in words)-y}
    if sum(len(x['text']) for x in lines) > MAX_TEXT:
        raise ValueError('Слишком много текста на листе; выберите другой лист')
    return lines


def recognize_page(path, page_number, workspace=None):
    from contextlib import nullcontext
    with (nullcontext(workspace) if workspace else tempfile.TemporaryDirectory(prefix='procurement-ocr-')) as directory:
        root = Path(directory)
        image, output = root / 'page', root / 'ocr'
        _run(['pdftoppm', '-f', str(page_number), '-l', str(page_number), '-singlefile',
              '-scale-to', '6000', '-png', str(path), str(image)], 18)
        png = image.with_suffix('.png')
        if not png.is_file() or png.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Изображение листа превышает безопасный размер')
        _run(['tesseract', str(png), str(output), '-l', 'rus+eng', '--psm', '11', 'tsv'], 30)
        lines = parse_tsv(output.with_suffix('.tsv'))
    if not lines:
        raise ValueError('На скане не распознан текст. Выберите лист со спецификацией или введите позиции вручную')
    return {'text': '\n'.join(x['text'] for x in lines), 'mode': 'ocr', 'lines': lines}


def _table_quantity(line, lines):
    """Read only the aligned quantity column, never drawing dimensions/mass."""
    box = line.get('bbox')
    if not box:
        return None
    same_page = [item for item in lines if item.get('page',1)==line.get('page',1) and item.get('bbox')]
    headers = [item for item in same_page if re.fullmatch(r'Кол\.?|Кол-во|Количество',item['text'],re.I)
               and item['bbox']['top'] < box['top']
               and 0 < item['bbox']['left']-box['left'] < 50*max(box['height'],1)
               and box['top']-item['bbox']['top'] < 70*max(box['height'],1)]
    for header in sorted(headers,key=lambda item:(-item['bbox']['top'],item['bbox']['left']-box['left'])):
        hb = header['bbox']; baseline = box['top']+box['height']/2
        name_headers = [item for item in same_page if re.fullmatch('Наименование',item['text'],re.I)
                        and abs(item['bbox']['top']-hb['top']) <= 2*hb['height']
                        and item['bbox']['left'] < box['left']+box['width'] < hb['left']]
        if not name_headers:
            continue
        # The nearest explicit quantity heading is required; neighboring mass
        # columns are excluded even if their numbers have higher confidence.
        center = hb['left']+hb['width']/2
        cells = [item for item in same_page if item is not line
                 and abs(item['bbox']['top']+item['bbox']['height']/2-baseline) <= max(box['height'],hb['height'])*.6
                 and abs(item['bbox']['left']+item['bbox']['width']/2-center) <= max(hb['width']*.9,hb['height'])]
        cells.sort(key=lambda item:item['bbox']['left'])
        if not cells:
            return ('',0.0)
        value = ''.join(item['text'].strip() for item in cells)
        confidence = min(item['confidence'] for item in cells)
        return (value if re.fullmatch(r'\d+(?:[,.]\d+)?',value) else '',confidence)
    return None


def candidate_rows(lines):
    """Prefill aligned table cells or explicit units; keep uncertainty visible."""
    rows = []
    for line in lines:
        # Section headings are not material positions. They remain in the
        # complete OCR transcript, alongside all unrecognized drawing text.
        if re.match(r'^Спецификация\b',line['text'],re.I):
            continue
        if re.fullmatch(r'Блоки\s+ФБС\.?',line['text'].strip(),re.I):
            continue
        if re.search(r'\b(?:ФБС|Панель|Кабель|Арматура|Блок|Труба|Калитка|Ворота)\b', line['text'], re.I):
            table_quantity = _table_quantity(line,lines)
            if table_quantity is not None:
                quantity, confidence = table_quantity
                # A product family establishes pieces, but malformed marks
                # and low-confidence cells always require explicit review.
                block = re.fullmatch(r'ФБС\s+\d{1,2}\.\d{1,2}\.\d{1,2}(?:[- ]?[А-ЯЁ])?',line['text'].strip(),re.I)
                trusted = bool(block and quantity and min(line['confidence'],confidence)>=.9)
                rows.append({'row':line['line'],'name':line['text'][:240],
                    'quantity':quantity,'unit':'шт' if block else '',
                    'specification':line['text'],'confidence':min(line['confidence'],confidence),
                    **({} if trusted else {'error':'Сверьте марку и количество с таблицей исходного PDF'})})
                continue
            match = re.fullmatch(
                r'(?P<name>.+?)\s+(?P<quantity>\d+(?:[,.]\d+)?)\s*'
                r'(?P<unit>шт\.?|штук|кг|тонн?|м|м2|м²|м3|м³|пог\.?\s*м|комплект(?:ов|а)?)\s*',
                line['text'].strip(), re.I)
            trusted = match is not None and line['confidence'] >= .9 and len(match['name'].strip()) >= 3
            rows.append({'row': line['line'],
                         'name': match['name'].strip()[:240] if trusted else line['text'][:240],
                         'quantity': match['quantity'] if trusted else '',
                         'unit': match['unit'] if trusted else '',
                         'specification': line['text'], 'confidence': line['confidence'],
                         **({} if trusted else {'error': 'Уточните наименование, количество и единицу по исходному PDF'})})
    return rows[:500]
