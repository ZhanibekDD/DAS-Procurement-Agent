"""Bounded local OCR for explicitly reviewed prices, never automatic accounting.

Columns are anchored by their printed headings. Quantities, totals, article
numbers and alternative unit prices cannot stand in for the selected price.
All OCR text is retained for review; uncertain cells remain blank.
"""
from dataclasses import replace
from datetime import date
from pathlib import Path
import re
import tempfile
import time

from .imports import (ExtractedItem, _explicit_currencies,
                      _vat_context, _pdf_price, _classify_document, extract_date)
from .pdf_ocr import MAX_PAGES, MAX_REVIEW_LINES
from .upload_io import FilePayload, open_payload

RECOGNITION_VERSION = 2
NAME = re.compile(r'^(?:Товар(?:\s*\(.*\))?|Наименование(?:\s+товара)?|Номенклатура|Продукция)$', re.I)
PRICE = re.compile(r'^Цена(?:\s|$)', re.I)
QUANTITY = re.compile(r'^(?:Количество|Кол[. -]*во|Кол\.)$', re.I)
TOTAL = re.compile(r'^(?:Сумма|Итого|Всего|В том числе|Оплата|Руководитель|Бухгалтер)\b', re.I)
UNIT = r'(?:шт\.?|м[³3]|м[²2]|кг|тонн[аы]?|т|м|рейс(?:ов|а)?|уп(?:ак)?\.?)'


def unit(value):
    text=value.casefold().replace('³','3').replace('²','2').rstrip('.')
    return {'тонна':'т','тонны':'т','рейсов':'рейс','рейса':'рейс','упак':'уп'}.get(text,text)


def center(cell,axis):
    b=cell['bbox'];return b['left' if axis=='x' else 'top']+b['width' if axis=='x' else 'height']/2


def scanned_items(lines, page, currency, vat):
    cells=[c for c in lines if c.get('bbox')]
    headings=[c for c in cells if NAME.fullmatch(c['text'].strip())]
    result=[]
    for heading in headings:
        height=max(heading['bbox']['height'],1);top=center(heading,'y')
        nearby=[c for c in cells if abs(center(c,'y')-top)<3*height and center(c,'x')>center(heading,'x')]
        prices=[c for c in nearby if PRICE.match(c['text'].strip())
                and not re.search(r'достав|итог|общ|shipping|total',c['text'],re.I)]
        quantities=[c for c in nearby if QUANTITY.fullmatch(c['text'].strip())]
        if not prices or len(quantities)!=1:
            continue
        quantity=quantities[0]
        if center(quantity,'x')>=min(center(c,'x') for c in prices):
            continue  # Unsupported layout remains in the full transcript.
        header_bottom=max(c['bbox']['top']+c['bbox']['height'] for c in [heading,quantity,*prices])
        right=(center(heading,'x')+center(quantity,'x'))/2
        # The printed quantity heading owns this column, not every numeric
        # cell between the description and price (which may include weight).
        qbox=quantity['bbox']
        quantity_left=qbox['left']-height
        quantity_right=min(qbox['left']+qbox['width']+height,
                           min(center(p,'x') for p in prices)-height)
        # Build price headings from vertical fragments inside each known column.
        specs=[]
        for price in prices:
            px=center(price,'x')
            fragments=[c for c in nearby if abs(center(c,'x')-px)<max(price['bbox']['width']/2,height)
                       and (c is price or re.fullmatch(r'(?:\d+\s*'+UNIT+r'\s*(?:с|без)?|(?:с|без)?\s*НДС)',c['text'].strip(),re.I))]
            label=' '.join(c['text'] for c in sorted(fragments,key=lambda c:center(c,'y')))
            header_bottom=max(header_bottom,max(c['bbox']['top']+c['bbox']['height'] for c in fragments))
            basis=re.search(r'(?<!\w)(\d+)\s*('+UNIT+r')(?!\w)',label,re.I)
            uncertain=any(c['confidence']<.9 for c in fragments if re.search(UNIT,c['text'],re.I)) or price['confidence']<.9
            specs.append((price,unit(basis[2]) if basis and basis[1]=='1' else '',bool(uncertain or basis and basis[1]!='1')))
        stops=[c['bbox']['top'] for c in cells if c['bbox']['top']>header_bottom and
               (TOTAL.match(c['text'].strip()) or (c is not heading and NAME.fullmatch(c['text'].strip())))]
        end=min(stops,default=float('inf'))
        # Headings can be centered over a wide column while values are
        # left-aligned. Its text edge is not the column edge. The preceding
        # printed header (number/article) bounds the name column instead.
        left_headers=[c for c in cells if abs(center(c,'y')-top)<height
                      and c['bbox']['left']+c['bbox']['width']<=heading['bbox']['left']]
        name_left=max((c['bbox']['left']+c['bbox']['width'] for c in left_headers),
                      default=heading['bbox']['left']-height)
        names=[c for c in cells if header_bottom<c['bbox']['top']<end and
               name_left<=c['bbox']['left']<right and re.search('[А-Яа-яA-Za-z]',c['text']) and
               not re.fullmatch(r'\d+(?:[.,]\d+)?\s*'+UNIT,c['text'].strip(),re.I)]
        for name in names:
            baseline=center(name,'y')
            aligned=[c for c in cells if c is not name and abs(center(c,'y')-baseline)<=height*.65]
            quantities_found=[]
            for c in aligned:
                if not quantity_left<=center(c,'x')<=quantity_right:
                    continue
                m=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*('+UNIT+r')',c['text'].strip(),re.I)
                if m:quantities_found.append((c,m[1],unit(m[2])))
            quantities_found.sort(key=lambda entry:entry[0]['bbox']['left'])
            # A merged quantity heading can contain piece and volume columns.
            # Preserve their printed order, but duplicate units are ambiguous.
            qty=quantities_found[0] if (quantities_found and quantity['confidence']>=.9
                and quantities_found[0][0]['confidence']>=.9
                and len({q[2] for q in quantities_found})==len(quantities_found)) else None
            selected=[spec for spec in specs if qty and spec[1]==qty[2] and not spec[2]]
            if not selected and len(specs)==1 and not specs[0][1] and not specs[0][2]:selected=specs
            amount='';warnings=['Скан: сверьте наименование, цену и единицу по оригиналу.']
            if len(selected)==1:
                p,_,_=selected[0];px=center(p,'x')
                spacing=min([abs(center(other,'x')-px)/2 for other in prices if other is not p] or [height*3])
                price_cells=[c for c in aligned if abs(center(c,'x')-px)<min(spacing,height*3)]
                if len(price_cells)==1 and price_cells[0]['confidence']>=.9:
                    try:amount=_pdf_price(price_cells[0]['text'],currency)
                    except ValueError:pass
            if not amount:warnings.append('Цена не подтверждена распознаванием: введите цену за выбранную единицу по оригиналу.')
            if len(specs)>1:warnings.append('В документе несколько ценовых колонок; цены за разные единицы не объединены.')
            if name['confidence']<.9:warnings.append('Проверьте написание марки: низкая уверенность OCR.')
            result.append(ExtractedItem(item_name=name['text'],normalized_name=name['text'].casefold(),brand='',
                quantity=qty[1] if qty else '',unit=qty[2] if qty else '',unit_price=amount,total_price='',
                currency=currency,vat_included=vat,source_page=page,source_sheet='',source_row=name['line'],
                source_cell='',source_text=' | '.join(c['text'] for c in sorted([name,*aligned],key=lambda c:c['bbox']['left'])),
                review_warning=' '.join(warnings)))
    return result


def document_date(text):
    months='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
    text=text.replace('«','').replace('»','').replace('"','')
    # A published price snapshot may omit an issue number/date but explicitly
    # identify the date of its prices. Do not substitute upload or mail dates.
    priced=re.search(r'\bЦены\s+указаны\s+на\s+(\d{1,2}[./-]\d{1,2}[./-](?:20\d{2}|\d{2}))(?!\d)',text,re.I)
    if priced:return extract_date(priced[1])
    # A validity deadline is not the issue date. Never read the first arbitrary
    # date in the footer, filename or bank details as the price date.
    label=re.search(r'(?:(?:Сч[её]т|Исх\.|КП|Предложение)[^\n]{0,85}?\bот\s+|\bДата(?:\s+(?:прайса|документа))?\s*:\s*)(\d{1,2}(?:[. /-]|\s)[^\n]{3,50})',text,re.I)
    if not label:return None
    value=label[1]
    m=re.match(r'(\d{1,2})\s+('+'|'.join(months)+r')\s+(20\d{2})(?!\d)',value,re.I)
    if m:
        try:return date(int(m[3]),months.index(m[2].lower())+1,int(m[1])).isoformat()
        except ValueError:return None
    numeric=re.match(r'\d{1,2}[./-]\d{1,2}[./-](?:20\d{2}|\d{2})(?!\d)',value)
    return extract_date(numeric[0]) if numeric else None


def seller_fields(text):
    from .regions import infer_region
    from .table_ingest import valid_inn
    # Bank, buyer and mail sender are not the seller. Only a labelled seller
    # block can prefill identity; missing/conflicting identifiers stay blank.
    block=re.search(r'Поставщик\s*:\s*(.+?)(?=Покупатель\s*:|Получатель\s*:|\nТовар|$)',text,re.I|re.S)
    if not block:return {}
    value=' '.join(block[1].split())
    inns=set(re.findall(r'ИНН\s*[: ]\s*(\d{10,12})(?!\d)',value,re.I))
    inn=next(iter(inns)) if len(inns)==1 and valid_inn(next(iter(inns))) else ''
    name=re.search(r'(?:Акционерное\s+общество|Общество\s+с\s+ограниченной\s+ответственностью|ООО|АО|ЗАО|ПАО)\s*["«]([^"»]+)["»]',value,re.I)
    return {'supplier_name':name[0] if name else '', 'supplier_tax_id':inn,
            'supplier_region':infer_region(value), 'supplier_email':'','supplier_phone':''}


def extract_price_document(content, filename):
    from .imports import extract_document
    result=extract_document(content,filename)
    if not getattr(result,'scan_context',[]):
        pages=getattr(result,'page_texts',[])
        if not pages:return result
        from .regions import infer_region
        text='\n'.join(pages)
        # Header address only; a delivery city in the footer is not the seller.
        header=re.split(r'Исх\.|Коммерческое предложение|Сч[её]т\s+на',text,1,flags=re.I)[0]
        return replace(result,document_date=document_date(text),
                       supplier_region=result.supplier_region or infer_region(header))
    if len(result.scan_context)>MAX_PAGES:
        raise ValueError('Сканированный прайс содержит более 12 страниц. Разделите его на части для проверки.')
    from .document_analysis import extract_pdf_page_review
    # EML imports may supply bytes. Native tools only receive a private file.
    with tempfile.TemporaryDirectory(prefix='price-ocr-') as folder:
        payload=content
        if not isinstance(content,FilePayload):
            path=Path(folder)/'original.pdf'
            with open_payload(content) as src, path.open('wb') as dst:
                import shutil
                shutil.copyfileobj(src,dst,64*1024)
            payload=FilePayload(path)
        pages=list(result.scan_context);review=[];ocr={};started=time.monotonic()
        for page,text in enumerate(pages,1):
            if not text.strip():
                if time.monotonic()-started>150:raise ValueError('Распознавание прайса заняло слишком долго; загрузите меньше страниц')
                extracted=extract_pdf_page_review(payload,page)
                pages[page-1]=extracted['text'];ocr[page]=extracted['lines']
                review.extend({**line,'page':page} for line in extracted['lines'])
            else:
                review.extend({'page':page,'line':n,'text':line,'confidence':1.0} for n,line in enumerate(text.splitlines(),1))
            if len(review)>MAX_REVIEW_LINES:raise ValueError('Слишком много строк прайса; разделите документ на части')
    text='\n'.join(pages)
    # Low-confidence stamp/signature marks (e.g. a spurious '$') cannot
    # establish financial currency. The complete transcript remains visible.
    currency_text='\n'.join(l['text'] for l in review if l['confidence']>=.9)
    currencies=_explicit_currencies(currency_text)
    currency=next(iter(currencies)) if len(currencies)==1 else ''
    try:vat=_vat_context(text)
    except ValueError:vat=None
    items=list(result.items)
    for page,lines in ocr.items():items.extend(scanned_items(lines,page,currency,vat))
    errors=['Скан распознан. До сохранения проверьте все строки и реквизиты по оригиналу.']
    if not currency:errors.append('Валюта не определена однозначно. Подтвердите цены в рублях; пересчёт не выполняется.')
    if vat is None:errors.append('НДС не определён однозначно; уточните условия по оригиналу.')
    if not items:errors.append('Ценовая таблица не распознана. Все строки OCR показаны ниже для ручного ввода.')
    sellers=[]
    for lines in ocr.values():
        labels=[c for c in lines if re.fullmatch(r'Поставщик\s*:',c['text'],re.I) and c.get('bbox')]
        for label in labels:
            box=label['bbox']
            values=[c for c in lines if c.get('bbox') and c['bbox']['left']>box['left']+box['width']
                    and box['top']-box['height']*1.5<c['bbox']['top']<box['top']+box['height']*2]
            if values:sellers.append(seller_fields('Поставщик: '+' '.join(c['text'] for c in sorted(values,key=lambda c:center(c,'y')))))
    seller=sellers[0] if sellers and all(s==sellers[0] for s in sellers) else seller_fields(text) if not sellers else {
        'supplier_name':'','supplier_tax_id':'','supplier_region':'','supplier_email':'','supplier_phone':''}
    if len(sellers)>1 and not seller.get('supplier_name'):
        errors.append('На страницах различаются реквизиты поставщика. Укажите поставщика отдельно для каждой строки.')
    return replace(result,document_type=_classify_document(text),document_date=document_date(text),
                   currency=currency,vat_included=vat,items=items,errors=errors,review_lines=review,
                   scan_context=[],**seller)
