"""Real HTTP gate using synthetic prices only in isolated canary, never Production."""
import csv
import hashlib
import io
import re
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage
from urllib.parse import urlsplit
import httpx
from procurement.catalog import ALIASES


def accept(url, credentials):
    assert urlsplit(url).hostname in {'localhost', '127.0.0.1'}
    checks = []
    def check(name, ok):
        assert ok, name
        checks.append(name)
    today = date.today()
    old, recent, expiry = [(today + timedelta(days=n)).isoformat() for n in (-25, -7, 60)]
    marks = ['ФБС 24.4.6', 'ФБС 12.4.6', 'ФБС 9.4.6']
    sources = {}
    with httpx.Client(base_url=url, timeout=120) as c:
        check('memory personal login', c.post('/auth/login', data=credentials).status_code == 303)
        c.headers['X-Launch-CSRF-Token'] = re.search(r'<meta name="procurement-launch-csrf" content="([a-f0-9]+)">', c.get('/').text)[1]
        def request(method, path, status=200, **kw):
            response = c.request(method, path, **kw)
            check('memory '+method+' '+path, response.status_code == status)
            return response.json()
        def raw_prices(price, supplier, day):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(ALIASES), delimiter=';')
            writer.writeheader()
            for mark in marks + ['ПБ 24.4.6']:
                writer.writerow(dict(item_name=mark, specification='ГОСТ 13579-2018', unit='шт.', unit_price=price,
                    currency='RUB', vat='с НДС', delivery='доставка включена', region='Воронежская область',
                    minimum_batch='10', price_date=day, valid_until=expiry, supplier_name='ТЕСТ Память '+supplier,
                    email='memory-'+supplier.lower()+'@example.test'))
            return stream.getvalue().encode()
        for amount, supplier, day, mail in [('90','A',old,False),('110','B',old,True),('112','A',recent,False),('128','B',recent,True)]:
            raw = raw_prices(amount, supplier, day)
            if mail:
                message = EmailMessage(); message['From'] = 'incoming@example.test';message.set_content('Прайс')
                message.add_attachment(raw, maintype='text', subtype='csv', filename='memory-price.csv')
                p = request('POST','/api/procurement/catalog/incoming-mail',files={'file':('memory.eml',message.as_bytes())})['previews'][0]
            else:
                p = request('POST','/api/procurement/catalog/preview', files={'file':('memory-price.csv',raw)})
            check('four validated immutable source rows', len(p['rows']) == 4 and not p['errors'])
            applied = request('POST',f"/api/procurement/catalog/{p['preview_id']}/apply",json={'confirmed':True})
            check('four new historical prices', applied['added'] == 4)
            sources[p['document_id']] = raw
        suppliers = request('GET','/api/suppliers')
        sid = next(s['id'] for s in suppliers if s['email'] == 'memory-b@example.test')
        project = request('POST','/api/projects',201,json={'name':'ТЕСТ Память ФБС','region':'Воронежская область','delivery_address':'Тестовый адрес'})
        lot = request('POST','/api/lots',201,json={'project_id':project['id'],'title':'ТЕСТ Память ФБС',
            'region':'Воронежская область','delivery_address':'Тестовый адрес','response_deadline':expiry,
            'items':[{'name':n,'quantity':q,'unit':'шт','specification':'ГОСТ 13579-2018'} for n,q in zip(marks,['218','95','128'])]})
        quote_raw = b'material;price\nFBS;128\n'
        doc = request('POST','/api/documents',201,params={'document_type':'commercial_offer','project_id':project['id'],'supplier_id':sid},files={'file':('memory-quote.csv',quote_raw)})
        sources[doc['id']] = quote_raw
        quote = request('POST',f"/api/lots/{lot['id']}/quotes",201,json={'supplier_id':sid,'currency':'RUB','vat_included':True,
            'delivery_basis':'included','price_date':recent,'valid_until':expiry,'source_document_id':doc['id'],
            'items':[{'lot_item_id':i['id'],'unit_price':'128','minimum_batch':'10','compliant':True} for i in lot['items']]})
        for mark in marks:
            for days in (30,90,365):
                data = request('GET','/api/procurement/price-memory',params={'q':mark,'specification':'ГОСТ 13579-2018','days':days})
                check('exact '+mark+' no PB '+str(days), data['total']==5 and all(r['item_name']==mark for r in data['records']))
                group = data['groups'][0]
                check('actual median and 20% dynamic '+mark+' '+str(days), len(data['groups'])==1 and group['current_stats']=={'count':2,'min':'112','median':'120','max':'128'} and group['change_pct']=='20.00')
                check('prices AND quote retained '+mark, {r['source_kind'] for r in data['records']}=={'price_list','quote'} and any(r['quote_id']==quote['id'] for r in data['records']))
                check('regional index separate from reliability '+mark, {r['price_index_pct'] for r in data['records'] if r['current']}=={'-6.67','6.67'} and all(r['reliability']==3 for r in data['records']))
            page = request('GET','/api/procurement/price-memory',params={'q':mark,'specification':'ГОСТ 13579-2018','limit':1})
            check('pagination preserves full stats',len(page['records'])==1 and page['total']==5 and page['groups'][0]['current_stats']['median']=='120')
        scoped = request('GET','/api/procurement/price-memory',params={'project_id':project['id']})
        check('project only exact three FBS brands', set(r['item_name'] for r in scoped['records'])==set(marks))
        data = request('GET','/api/procurement/price-memory',params={'q':'ФБС','specification':'ГОСТ 13579-2018','days':30})
        check('price rise notifications', any(a['kind']=='price_rise' for a in data['alerts']))
        eid = data['alerts'][0]['event_id']
        request('POST','/api/procurement/price-memory/alerts/read',json={'event_ids':[eid]})
        after = request('GET','/api/procurement/price-memory',params={'q':'ФБС','specification':'ГОСТ 13579-2018','days':30})
        check('server-scoped notification acknowledgement', next(a for a in after['alerts'] if a['event_id']==eid)['read'])
        for did, raw in sources.items():
            path=f'/api/launch/documents/{did}/download'
            check('source immutable bytes '+str(did), hashlib.sha256(c.get(path).content).digest()==hashlib.sha256(raw).digest())
            check('source HEAD '+str(did),c.head(path).status_code==200)
            check('source Range '+str(did),c.get(path,headers={'Range':'bytes=0-15'}).content==raw[:16])
            check('source CSV opens '+str(did),'<table>' in c.get(f'/api/procurement/documents/{did}/view').text)
            for method in ('GET','HEAD'):
                check('anonymous source denied '+method,httpx.request(method,url+path).status_code==403)
            check('forged source Range denied',httpx.get(url+path,headers={'Range':'bytes=0-15','X-OpenWebUI-User-Id':'admin'}).status_code==403)
        # This canary uses the same legacy session contour as Production (403).
        # The separately tested SSO adapter returns 401 before route dispatch.
        check('unauthenticated memory denied',httpx.get(url+'/api/procurement/price-memory').status_code==403)
        check('memory service health',c.get('/health').status_code==200)
    return checks
