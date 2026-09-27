"""Authenticated, escaped, bounded Office previews. Originals remain immutable."""
import html
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .table_ingest import read_table
from .upload_io import open_payload


def office_html(content,filename,sheet=''):
    suffix=Path(filename).suffix.lower()
    if suffix in {'.xlsx','.csv'}:
        # The reader marks formulas explicitly; never evaluates or follows external links.
        table=read_table(content,filename,sheet)
        rows=[table['headers']]+[r['cells'] for r in table['rows'][:1000]]
        links=' '.join('<a href="?sheet='+html.escape(__import__('urllib.parse',fromlist=['quote']).quote(s))+'">'+html.escape(s)+'</a>' for s in table['sheets'])
        body=links+'<p>Предпросмотр первых 1000 строк; оригинал доступен целиком. Формулы показаны как формулы, не вычисляются.</p><table>'+''.join('<tr>'+''.join('<td>'+html.escape('[формула]' if isinstance(c,dict) else str(c))+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'
    elif suffix=='.docx':
        with open_payload(content) as stream,zipfile.ZipFile(stream) as archive:
            info=archive.getinfo('word/document.xml')
            if info.file_size>8*1024*1024:
                raise ValueError('DOCX слишком большой для предпросмотра; скачайте оригинал')
            data=archive.read(info)
        if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
            raise ValueError('Недопустимая структура DOCX')
        root=ET.fromstring(data);ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        body='<p>Просмотр текста и таблиц. Исходное оформление сохранено в оригинале.</p>'
        for node in root.findall('.//w:body/*',ns):
            if node.tag.endswith('}tbl'):
                body+='<table>'+''.join('<tr>'+''.join('<td>'+html.escape(''.join(t.text or '' for t in cell.findall('.//w:t',ns)))+'</td>' for cell in row.findall('w:tc',ns))+'</tr>' for row in node.findall('w:tr',ns))+'</table>'
            else:body+='<p>'+html.escape(''.join(t.text or '' for t in node.findall('.//w:t',ns)))+'</p>'
    else:raise ValueError('Для этого формата нет Office-предпросмотра')
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><title>'+html.escape(filename)+'</title><style>body{font:15px system-ui;padding:24px;color:#203040}table{border-collapse:collapse}td{border:1px solid #ddd;padding:7px;white-space:pre-wrap}p{white-space:pre-wrap}a{margin-right:12px}</style>'+body+'</html>'
