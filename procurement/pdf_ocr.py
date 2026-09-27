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
        for row in csv.DictReader(stream, delimiter='\t'):
            word = (row.get('text') or '').strip()
            if not word or row.get('level') != '5':
                continue
            try:
                key = tuple(int(row[k]) for k in ('block_num', 'par_num', 'line_num'))
                confidence = float(row['conf'])
                left, top, height = (int(row[k]) for k in ('left', 'top', 'height'))
            except (KeyError, ValueError) as exc:
                raise ValueError('OCR вернул некорректные данные; заявка не создана') from exc
            groups.setdefault(key, []).append((left, top, height, word, confidence))
            if len(groups) > MAX_LINES:
                raise ValueError('Слишком много строк на листе; выберите другой лист')
    lines = []
    for words in sorted(groups.values(), key=lambda w: (min(x[1] for x in w), min(x[0] for x in w))):
        words.sort(key=lambda w: w[0])
        text = ' '.join(w[3] for w in words)
        lines.append({'line': len(lines) + 1, 'text': text,
                      'confidence': round(min(w[4] for w in words) / 100, 3)})
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


def candidate_rows(lines):
    """Drafts only: never infer a quantity from drawing dimensions or a mass column."""
    rows = []
    for line in lines:
        if re.search(r'\b(?:ФБС|Панель|Кабель|Арматура|Блок|Труба|Калитка|Ворота)\b', line['text'], re.I):
            rows.append({'row': line['line'], 'name': line['text'][:240], 'quantity': '',
                         'unit': '', 'specification': line['text'], 'confidence': line['confidence'],
                         'error': 'Уточните наименование, количество и единицу по исходному PDF'})
    return rows[:500]
