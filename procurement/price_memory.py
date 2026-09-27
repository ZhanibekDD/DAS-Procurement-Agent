"""Global read-through price memory. No fuzzy matching or rewriting of sources."""
import hashlib
import json
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from .identity import trusted_actor

SCHEMA = '''CREATE TABLE IF NOT EXISTS price_memory_alert_reads (
 actor TEXT NOT NULL,event_id TEXT NOT NULL,read_at TEXT NOT NULL,
 PRIMARY KEY(actor,event_id));'''


def normalized(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold().replace('ё', 'е')
    return ' '.join(value.split())


def material_key(value):
    value = normalized(value)
    # Only known material designations receive punctuation/spacing aliases.
    # Family is part of the key: an FBS mark can never match a PB mark.
    match = re.search(r'(?<!\w)(фбс|пб|фл)\s*[-–—]?\s*(\d+)\s*[.хx×-]\s*(\d+)\s*[.хx×-]\s*(\d+)(?!\d)', value)
    primary = re.search(r'(?<!\w)(фбс|пб|фл)(?!\w)', value)
    if not match or (primary and primary[1] != match[1]):
        return value
    before = value[:match.start()].strip()
    if before in {'блок', 'блоки', 'блок фундаментный', 'блоки фундаментные', 'плита', 'плиты'}:
        before = ''
    mark = match[1] + ' ' + '.'.join(str(int(match[i])) for i in (2, 3, 4))
    return ' '.join(filter(None, (before, mark, value[match.end():].strip())))


INVALID_DESIGNATION = '<conflicting designation>'


def designation_key(name, specification=''):
    """Require one complete mark across both fields; variants and family matter."""
    pattern = (r'(?<!\w)(фбс|пб|фл)\s*[-–—]?\s*(\d+)\s*[.хx×-]\s*(\d+)\s*[.хx×-]\s*(\d+)'
               r'(?:\s*[-–—]\s*([а-яa-z]{1,3}))?(?!\w)')
    fields = (normalized(name), normalized(specification))
    found = [match for field in fields for match in re.finditer(pattern, field)]
    families = ({match[1] for match in found}
                | {match[1] for field in fields for match in re.finditer(r'(?<!\w)(фбс|пб|фл)(?!\w)', field)})
    marks = {match[1] + ' ' + '.'.join(str(int(match[i])) for i in (2, 3, 4))
             + ('-' + match[5] if match[5] else '')
             for match in found}
    if len(families) > 1 or len(marks) > 1 or (marks and families != {next(iter(marks)).split()[0]}):
        return INVALID_DESIGNATION
    return next(iter(marks)) if marks else None


def unit_key(value):
    value = normalized(value).rstrip('.')
    return {'штука':'шт', 'штуки':'шт', 'штук':'шт', 'pcs':'шт', 'pc':'шт',
            'м2':'м²', 'm2':'м²', 'м3':'м³', 'm3':'м³', 'тонна':'т', 'тонны':'т',
            'тонн':'т', 'кг':'кг', 'килограмм':'кг', 'метр':'м', 'метры':'м'}.get(value, value)


def vat_key(value):
    value = normalized(value)
    return {'ндс включен':'с ндс', 'ндс не включен':'без ндс'}.get(value, value)


def delivery_key(value):
    value = normalized(value)
    return {'доставка включена':'included', 'включена':'included', 'самовывоз':'pickup',
            'бесплатная доставка':'included'}.get(value, value)


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def pct(value, baseline):
    return str(((value / baseline - 1) * 100).quantize(Decimal('0.01'))) if baseline else None


def stats(values):
    return {'count':len(values), 'min':str(min(values)), 'median':str(statistics.median(values)),
            'max':str(max(values))} if values else {'count':0, 'min':None, 'median':None, 'max':None}


def basis(row):
    return (material_key(row['item_name']), normalized(row['specification']), unit_key(row['unit']),
            row['currency'], vat_key(row['vat']), normalized(row['region']),
            delivery_key(row['delivery']), format(decimal(row['minimum_batch']).normalize(),'f') if decimal(row['minimum_batch']) is not None else '')


def comparable(row):
    return (bool(row['supplier_id'] and row['active'] and row['compliant']) and bool(row['unit'])
            and bool(row['region']) and bool(row['valid_until'])
            and bool(re.fullmatch(r'(с ндс|без ндс|(?:ндс\s*)?\d{1,2}(?:[.,]\d+)?\s*%)', vat_key(row['vat'])))
            and delivery_key(row['delivery']) not in {'', 'не указано', 'не определено', 'неизвестно', 'по согласованию'}
            and decimal(row['minimum_batch']) is not None)


# Source rows are returned by immutable PK, never resolved by a filename guess.
SOURCE_SQL = '''
SELECT 'catalog:'||p.id AS record_id,p.supplier_id,s.name AS supplier_name,s.rating AS reliability,s.active,
 p.item_name,p.specification,p.unit,p.unit_price,p.currency,p.vat,p.delivery,p.region,p.minimum_batch,
 p.price_date,p.valid_until,p.created_at,p.source_document_id,d.filename AS source_filename,
 'price_list' AS source_kind,p.source_sheet,p.source_row,NULL AS quote_id,NULL AS lot_id,
 d.project_id,1 AS compliant
 FROM supplier_catalog_prices p JOIN suppliers s ON s.id=p.supplier_id
 JOIN source_documents d ON d.id=p.source_document_id
UNION ALL
SELECT 'quote:'||i.id,q.supplier_id,s.name,s.rating,s.active,l.name,l.specification,l.unit,i.unit_price,
 q.currency,CASE q.vat_included WHEN 1 THEN 'с НДС' WHEN 0 THEN 'без НДС' ELSE '' END,
 CASE q.delivery_basis WHEN 'included' THEN 'included' WHEN 'pickup' THEN 'pickup'
 WHEN 'extra' THEN 'extra shipment: '||q.delivery_cost ELSE '' END,
 lot.region,i.minimum_batch,COALESCE(q.price_date,substr(q.created_at,1,10)),COALESCE(q.valid_until,''),
 q.created_at,q.source_document_id,COALESCE(d.filename,q.source_filename),'quote','',NULL,q.id,lot.id,
 lot.project_id,i.compliant
 FROM quote_items i JOIN quotes q ON q.id=i.quote_id JOIN lot_items l ON l.id=i.lot_item_id
 JOIN lots lot ON lot.id=q.lot_id JOIN suppliers s ON s.id=q.supplier_id
 LEFT JOIN source_documents d ON d.id=q.source_document_id
UNION ALL
SELECT 'import:'||p.id,p.supplier_id,COALESCE(s.name,sd.name,''),s.rating,COALESCE(s.active,0),
 p.item_name,p.brand,p.unit,p.unit_price,p.currency,
 CASE p.vat_included WHEN 1 THEN 'с НДС' WHEN 0 THEN 'без НДС' ELSE '' END,
 '',COALESCE(s.region,sd.region,''),'',COALESCE(p.document_date,''),COALESCE(p.valid_until,''),p.created_at,
 p.source_document_id,d.filename,'import',p.source_sheet,p.source_row,NULL,NULL,d.project_id,1
 FROM price_history_entries p LEFT JOIN suppliers s ON s.id=p.supplier_id
 LEFT JOIN supplier_drafts sd ON sd.id=p.supplier_draft_id LEFT JOIN source_documents d ON d.id=p.source_document_id
 WHERE p.status='confirmed'
UNION ALL
SELECT 'purchase:'||p.id,p.supplier_id,COALESCE(s.name,''),s.rating,COALESCE(s.active,0),p.item_name,'',p.unit,
 p.unit_price,p.currency,CASE p.vat_included WHEN 1 THEN 'с НДС' WHEN 0 THEN 'без НДС' ELSE '' END,
 '',p.region,'',p.purchased_on,'',p.created_at,p.source_document_id,d.filename,'purchase','',NULL,NULL,NULL,
 d.project_id,1 FROM purchase_history p LEFT JOIN suppliers s ON s.id=p.supplier_id
 LEFT JOIN source_documents d ON d.id=p.source_document_id WHERE p.review_status='approved'
'''


class PriceMemory:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def search(self, query='', specification='', region='', days=90, project_id=None, offset=0, limit=100, today=None):
        if days not in (30,90,365) or not 0 <= offset <= 1000000 or not 1 <= limit <= 200:
            raise ValueError('Период: 30/90/365 дней; страница: до 200 записей')
        if any(len(v)>800 for v in (query,specification,region)):
            raise ValueError('Слишком длинный поиск')
        today = today or date.today()
        start = today-timedelta(days=days-1)
        project_materials = None
        if project_id is not None:
            from .catalog import Catalog
            self.service.get_project(project_id)
            # Includes reviewed reference workbook materials, not only procurement lots.
            data = Catalog(self.service,None).portfolio(project_id)
            project_materials = {(material_key(i['name']), designation_key(i['name'],i.get('specification','')))
                                 for i in data['materials']
                                 if designation_key(i['name'],i.get('specification','')) != INVALID_DESIGNATION}
        query_key = material_key(query)
        family_query = re.search(r'(?<!\w)(фбс|пб|фл)(?!\w)', query_key)
        def matches(name, spec, reg):
            key = material_key(name)
            combined = material_key(str(name)+' '+str(spec))
            if project_materials is not None:
                designation = designation_key(name,spec)
                if designation == INVALID_DESIGNATION or not any(
                    (designation == project_mark if designation or project_mark else key == project_name)
                           for project_name,project_mark in project_materials):
                    return False
            if family_query:
                primary = re.search(r'(?<!\w)(фбс|пб|фл)(?!\w)', combined)
                if not primary or primary[1] != family_query[1]:return False
            return (not query_key or query_key == key or re.search(r'(?<!\w)'+re.escape(query_key)+r'(?!\w)',combined) is not None) and normalized(specification) in normalized(spec) and (not region or normalized(region)==normalized(reg))
        records=[]; groups=defaultdict(list); total=0; unread=[]
        with self.db.connection() as conn:
            conn.create_function('memory_matches',3,lambda n,s,r:int(matches(n,s,r)),deterministic=True)
            cursor=conn.execute('SELECT * FROM ('+SOURCE_SQL+') WHERE memory_matches(item_name,specification,region)=1 ORDER BY price_date DESC,created_at DESC,CAST(substr(record_id,instr(record_id,\':\')+1) AS INTEGER) DESC,record_id DESC')
            for source in cursor:
                row=dict(source); price=decimal(row['unit_price'])
                if price is None:
                    unread.append(row['record_id']);continue
                row['normalized_name']=material_key(row['item_name']);row['normalized_unit']=unit_key(row['unit'])
                try:
                    price_date=date.fromisoformat(row['price_date'])
                    valid_until=date.fromisoformat(row['valid_until']) if row['valid_until'] else None
                except (ValueError,TypeError):
                    price_date=valid_until=None
                row['comparable']=bool(comparable(row) and price_date and valid_until and valid_until>=price_date)
                row['freshness']='future' if price_date and price_date>today else 'expired' if valid_until and valid_until<today else 'unknown' if not valid_until or not price_date else 'valid'
                row['source_url']=f"/api/launch/documents/{row['source_document_id']}/download" if row['source_document_id'] else None
                row['source_view_url']=f"/api/procurement/documents/{row['source_document_id']}/view" if row['source_document_id'] else None
                row['quote_url']=f"/api/lots/{row['lot_id']}/quotes" if row['quote_id'] else None
                row['current']=False;row['price_index_pct']=None
                if offset<=total<offset+limit:records.append(row)
                total+=1
                # One history per source/supplier; source is preserved even if two
                # workflows registered the same original. Avoid double weighting.
                if row['comparable']:
                    groups[basis(row)].append((row,price_date,valid_until,price))
            reads={r[0] for r in conn.execute('SELECT event_id FROM price_memory_alert_reads WHERE actor=?',(trusted_actor(),))}
        results=[]; alerts=[]; current_ids={}; index_by_id={}
        for key, history in groups.items():
            history.sort(key=lambda h:(h[1],h[0]['created_at'],int(h[0]['record_id'].split(':')[1]),h[0]['record_id']))
            source_offers=defaultdict(list)
            for h in history:
                if h[0]['source_document_id']:
                    source_offers[(h[0]['supplier_id'],h[0]['source_document_id'])].append(h)
            duplicate_ids=set(); ambiguous_source_ids=set()
            for offers in source_offers.values():
                # One original is not a new price event just because another
                # workflow registered it. Conflicting readings are not a trend.
                if len({h[3] for h in offers})>1:
                    ambiguous_source_ids.update(h[0]['record_id'] for h in offers)
                else:
                    duplicate_ids.update(h[0]['record_id'] for h in offers[1:])
            for h in history:
                h[0]['duplicate_source']=h[0]['record_id'] in duplicate_ids
                h[0]['ambiguous_source']=h[0]['record_id'] in ambiguous_source_ids
            history=[h for h in history if h[0]['record_id'] not in duplicate_ids]
            ties=defaultdict(list)
            for h in history:ties[(h[0]['supplier_id'],h[1],h[0]['created_at'])].append(h)
            conflicts={t for t,hs in ties.items() if len({h[0]['source_kind'] for h in hs})>1 and len({h[3] for h in hs})>1}
            def ambiguous(h):return h[0]['ambiguous_source'] or (h[0]['supplier_id'],h[1],h[0]['created_at']) in conflicts
            for h in history:
                h[0]['ambiguous_order']=ambiguous(h)
            latest={}; events=defaultdict(list)
            for item in history:
                row,pdate,until,price=item
                if pdate<=today:
                    latest[row['supplier_id']]=item
                    events[pdate].append(item)
            current=[h for h in latest.values() if h[2]>=today and not ambiguous(h)]
            current_stat=stats([h[3] for h in current])
            median=Decimal(current_stat['median']) if current_stat['count']>=2 else None
            for row,_,_,price in current:
                current_ids[row['record_id']]=True;index_by_id[row['record_id']]=pct(price,median)
            # Snapshot median is carried forward ONLY while that offer is valid.
            dates={start,today}|{d for d in events if start<=d<=today}|{h[2]+timedelta(days=1) for h in history if start<=h[2]+timedelta(days=1)<=today}
            timeline=[]; active={}; event_dates=sorted(events); position=0
            for day in sorted(dates):
                while position<len(event_dates) and event_dates[position]<=day:
                    for h in events[event_dates[position]]:active[h[0]['supplier_id']]=h
                    position+=1
                point=stats([h[3] for h in active.values() if h[2]>=day and not ambiguous(h)])
                timeline.append({'date':day.isoformat(),**point})
            nonempty=[p for p in timeline if p['median'] is not None]
            change=pct(Decimal(nonempty[-1]['median']),Decimal(nonempty[0]['median'])) if len(nonempty)>1 else None
            group_id=hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()[:20]
            example=history[-1][0]
            result={'group_id':group_id,'item_name':example['item_name'],'specification':example['specification'],
                    'unit':key[2],'currency':key[3],'vat':key[4],'region':example['region'],'delivery':key[6],
                    'minimum_batch':key[7],'current_stats':current_stat,
                    'period_stats':stats([h[3] for h in history if start<=h[1]<=today and not ambiguous(h)]),'timeline':timeline,
                    'change_pct':change,'change_from':nonempty[0]['date'] if nonempty else None,
                    'change_to':nonempty[-1]['date'] if nonempty else None,
                    'suppliers':[{'record_id':h[0]['record_id'],'supplier_id':h[0]['supplier_id'],
                                  'supplier_name':h[0]['supplier_name'],'reliability':h[0]['reliability'],
                                  'price':str(h[3]),'price_index_pct':pct(h[3],median)} for h in current]}
            results.append(result)
            previous={}
            for h in history:
                row,day,until,price=h
                if day>today:continue
                sid=row['supplier_id'];old=previous.get(sid)
                prior_best=min((h[3] for h in previous.values() if h[2]>=day and not ambiguous(h)),default=None)
                kind=None;baseline=None
                if old and not ambiguous(old) and price>old[3]:kind='price_rise';baseline=old[3]
                elif prior_best is not None and price<prior_best:kind='better_offer';baseline=prior_best
                if kind and start<=day<=today and until>=today and not ambiguous(h):
                    rid=row['record_id'];eid=hashlib.sha256((kind+group_id+rid).encode()).hexdigest()
                    alerts.append({'event_id':eid,'kind':kind,'read':eid in reads,'date':day.isoformat(),
                        'record_id':rid,'item_name':row['item_name'],'supplier_name':row['supplier_name'],
                        'region':row['region'],'currency':row['currency'],'unit':key[2],
                        'old_price':str(baseline),'new_price':str(price),'change_pct':pct(price,baseline),
                        'source_url':row['source_url']})
                previous[sid]=h
        for row in records:
            row['current']=row['record_id'] in current_ids
            row['price_index_pct']=index_by_id.get(row['record_id'])
        results.sort(key=lambda r:(material_key(r['item_name']),r['specification'],r['region'],r['group_id']))
        alerts.sort(key=lambda a:(a['date'],a['event_id']),reverse=True)
        return {'query':query,'project_id':project_id,'days':days,'from':start.isoformat(),'to':today.isoformat(),
                'total':total,'offset':offset,'limit':limit,'records':records,'groups':results,
                'alerts':alerts,'unread_alerts':sum(not a['read'] for a in alerts),'invalid_records':unread,
                'basis_notice':'Сравниваются одинаковые товары, характеристики, единицы, валюта, НДС, регион, доставка и партия. Неизвестные условия — только история, без индекса.'}
