import pytest
from procurement.imports import _pdf_price, _price_table_header

def test_two_tier_price_header():
    rows=[['Артикул','Номенклатура','Вес (кг)','Объем (м3)','Прейскурантная с НДС',''],
          ['','','','','Цена','Ед.'], ['529000','1ПГ48-8','527','0.211','8,897.46 руб.','шт']]
    assert _price_table_header(rows)==(1,(1,4,None,5))

@pytest.mark.parametrize('title_col',[0,1,2])
@pytest.mark.parametrize('price_header',['Цена','Цена, руб.','Стоимость с НДС, руб.'])
def test_merged_title_cannot_assign_article_number_as_price(monkeypatch,title_col,price_header):
    from types import SimpleNamespace
    from procurement.imports import _extract_items_from_pdf_tables
    import pdfplumber
    title=['','',''];title[title_col]='Прайс-лист на 2026 год'
    rows=[title,['Артикул','Наименование',price_header],['529000','ФБС 24.4.6','8897']]
    assert _price_table_header(rows)==(1,(1,2,None,None))
    class PDF:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    table=SimpleNamespace(columns=[SimpleNamespace(bbox=(n*100,0,(n+1)*100,100)) for n in range(3)],extract=lambda:rows)
    pdf=PDF();pdf.pages=[SimpleNamespace(chars=[],close=lambda:None,find_tables=lambda:[table])]
    monkeypatch.setattr(pdfplumber,'open',lambda *args:pdf)
    items,found=_extract_items_from_pdf_tables(b'%PDF-synthetic','RUB',True)
    assert found and len(items)==1
    assert items[0].unit_price=='8897' and items[0].item_name=='ФБС 24.4.6'
    assert items[0].source_row==3 and '529000' in items[0].source_text

@pytest.mark.parametrize('title',[['Прайс-лист','','','',''],['Прайс-лист','АО Завод','','','']])
def test_title_then_split_header_keeps_quantity_unit_and_price(title):
    rows=[title,['Артикул','Номенклатура','Кол-во','Прейскурантная с НДС',''],
          ['','','','Цена','Ед.'],['529000','ФБС','2','8897','шт']]
    assert _price_table_header(rows)==(2,(1,3,2,4))

def test_complete_header_overrides_earlier_title_tier():
    rows=[['Прайс-лист','АО Завод','',''],['Артикул','Наименование','Цена, руб.','Ед.'],['529000','ФБС','8897','шт']]
    assert _price_table_header(rows)==(1,(1,2,None,3))

@pytest.mark.parametrize('raw,expected',[('8,897.46 руб.','8897.46'),('1 093,12 руб.','1093.12'),('1.093,12 ₽','1093.12'),('1093.12','1093.12')])
def test_explicit_pdf_prices(raw,expected):
    assert _pdf_price(raw,'RUB')==expected

@pytest.mark.parametrize('raw',['1,2.34 руб.','-12 руб.','0 руб.','12 USD','стоимость по запросу'])
def test_unsafe_pdf_price_is_not_guessed(raw):
    with pytest.raises(ValueError):_pdf_price(raw,'RUB')

def test_mass_and_volume_are_not_prices():
    assert _price_table_header([['Номенклатура','Вес (кг)','Объем'],['ФБС','527','0.211']]) is None

def test_header_not_built_from_content():
    assert _price_table_header([['Номенклатура','Кол-во'],['Цена работ','527']]) is None

@pytest.mark.parametrize('label',['Договорная','Договорная цена','по запросу','Стоимость по запросу','по согласованию'])
@pytest.mark.parametrize('suffix',['',' руб.',' ₽',' RUB'])
def test_negotiated_price_retains_row_for_review_without_inventing_price(monkeypatch,label,suffix):
    label += suffix
    from types import SimpleNamespace
    from procurement.imports import _extract_items_from_pdf_tables
    import pdfplumber
    class PDF:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def table(rows):
        return SimpleNamespace(columns=[SimpleNamespace(bbox=(n*100,0,(n+1)*100,100)) for n in range(3)],extract=lambda:rows)
    def page(rows):
        return SimpleNamespace(chars=[],close=lambda:None,find_tables=lambda:[table(rows)])
    pdf=PDF();pdf.pages=[page([['Номенклатура','Цена','Ед.'],['ФБС 24.4.6','8,897.46 руб.','шт'],
                              ['ФБС 12.4.6',label,'шт']]),
                        page([['ФБС 9.4.6',label,'шт'],['ПБ 30.12','1 234,50 руб.','шт']])]
    monkeypatch.setattr(pdfplumber,'open',lambda *args:pdf)
    items,found=_extract_items_from_pdf_tables(b'%PDF-synthetic','RUB',True)
    assert found and len(items)==4
    assert [i.unit_price for i in items]==['8897.46','','','1234.50']
    assert [i.source_page for i in items]==[1,1,2,2]
    assert [bool(i.review_warning) for i in items]==[False,True,True,False]
    assert label in items[1].source_text and 'Уточните цену' in items[1].review_warning

@pytest.mark.parametrize('label',['Договорная USD','по запросу EUR','Договорная руб. USD'])
def test_negotiated_price_does_not_erase_foreign_currency(label):
    from procurement.imports import _reviewable_pdf_price
    with pytest.raises(ValueError):_reviewable_pdf_price(label,'RUB')


def test_review_does_not_silently_import_empty_negotiated_price(workflow):
    from procurement.catalog import Catalog
    from procurement.imports import ExtractedItem,DocumentExtractResult
    db,service,launch=workflow
    doc=service.register_source_document(filename='price.csv',content=b'item;price\nx;1\n',document_type='price_list')
    result=DocumentExtractResult('price.pdf','0'*64,'price_list','ТЕСТ прайс','7707083893','Воронежская область','','','',
                                '2026-09-30',None,'RUB',True,
                                [ExtractedItem('ФБС 24.4.6','фбс','', '', 'шт','','','RUB',True,1,'',2,'','Договорная','Уточните цену')])
    catalog=Catalog(service,launch);preview=catalog.extracted_price_preview(doc,result)
    assert preview['rows'][0]['unit_price']=='' and preview['rows'][0]['review_warning']=='Уточните цену'
    checked=catalog.review_pdf(preview['preview_id'],preview['rows'])
    assert not checked['rows'] and checked['errors']
    assert not db.all('SELECT * FROM supplier_catalog_prices')

from test_launch_workflow import workflow
