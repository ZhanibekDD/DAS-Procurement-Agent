import pytest
from procurement.imports import _pdf_price, _price_table_header

def test_two_tier_price_header():
    rows=[['Артикул','Номенклатура','Вес (кг)','Объем (м3)','Прейскурантная с НДС',''],
          ['','','','','Цена','Ед.'], ['529000','1ПГ48-8','527','0.211','8,897.46 руб.','шт']]
    assert _price_table_header(rows)==(1,(1,4,None,5))

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
