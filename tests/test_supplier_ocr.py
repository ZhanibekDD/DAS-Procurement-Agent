import io
import json
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

from procurement.document_analysis import extract_pdf_page_review
from procurement.pdf_ocr import candidate_rows,parse_tsv
from procurement.upload_io import FilePayload,payload_sha256
from procurement.models import SupplierCreate
from procurement.service import ConflictError
from test_launch_workflow import workflow,project,lot_payload

FIXTURE=Path(__file__).parent/'fixtures/russian_scan.pdf'


def test_legacy_supplier_unrelated_edit_preserves_invalid_unchanged_inn(workflow):
    db,service,launch=workflow
    created=service.create_supplier(SupplierCreate(name='Старая карточка',region='Воронеж',tax_id='000000000001',phone='+74732000001'))
    current=launch.supplier(created['id'])
    fields={k:current[k] for k in SupplierCreate.model_fields};fields['name']='Обновлённая карточка'
    edited=launch.edit_supplier(created['id'],fields,current['revision'])
    assert edited['name']==fields['name'] and edited['tax_id']=='000000000001'
    fields['tax_id']='1234567890'
    with pytest.raises(ValueError,match='ИНН'):launch.edit_supplier(created['id'],fields,edited['revision'])
    assert service.get_supplier(created['id'])['tax_id']=='000000000001'
    assert len(db.all("SELECT * FROM audit_log WHERE action='supplier_edited'"))==1


def test_existing_conflicting_region_cluster_is_not_migrated_by_edit(workflow):
    db,service,launch=workflow
    supplier=service.create_supplier(SupplierCreate(name='Старая региональная карточка',region='Воронежская область'))
    with db.connection() as conn:conn.execute("UPDATE suppliers SET cluster='cluster_1' WHERE id=?",(supplier['id'],))
    current=launch.supplier(supplier['id']);fields={k:current[k] for k in SupplierCreate.model_fields}
    fields['name']='Новое название'
    edited=launch.edit_supplier(supplier['id'],fields,current['revision'])
    assert edited['cluster']=='cluster_1' and edited['name']==fields['name']
    assert edited['region']=='Воронежская область'


@pytest.mark.parametrize('phone',['+7 (473) 200–00–01','+7\u00a0(473)\u202f200‑00‑01','+7 (473) 200-00-01'])
def test_contact_typography_is_not_a_new_number(phone):
    from procurement.table_ingest import contacts
    assert contacts({'phone':phone})['phone']=='+74732000001'
    for invalid in ['+7 473 2000001 или +7 473 2000002','<script>1234567890','abc1234567890']:
        with pytest.raises(ValueError):contacts({'phone':invalid})


def test_no_quantity_guessed_from_drawing_dimensions():
    rows=candidate_rows([{'line':1,'text':'ФБС 24.4.6 179 995','confidence':.72}])
    assert rows[0]['quantity']=='' and rows[0]['unit']==''
    assert rows[0]['specification']=='ФБС 24.4.6 179 995'


def test_explicit_high_confidence_quantities_prefill_without_losing_codes():
    rows=candidate_rows([
        {'line':1,'text':'ФБС 24.4.6 218 шт','confidence':.96},
        {'line':2,'text':'Кабель 001230040500 10 м','confidence':.918},
        {'line':3,'text':'ФБС 12.4.6 95 шт','confidence':.84},
    ])
    assert [(row['name'],row['quantity'],row['unit']) for row in rows]==[
        ('ФБС 24.4.6','218','шт'),('Кабель 001230040500','10','м'),
        ('ФБС 12.4.6 95 шт','','')]
    assert rows[0]['specification']=='ФБС 24.4.6 218 шт'
    assert rows[2]['error'] and not rows[0].get('error')


def test_tsv_preserves_all_lines_and_marks_uncertainty(tmp_path):
    path=tmp_path/'scan.tsv'
    path.write_text('level\tblock_num\tpar_num\tline_num\tleft\ttop\theight\tconf\ttext\n'
                    '5\t1\t1\t1\t10\t10\t20\t90\tФБС\n'
                    '5\t1\t1\t1\t50\t10\t20\t40\t24.4.6\n'
                    '5\t2\t1\t1\t10\t50\t20\t30\t???\n',encoding='utf8')
    result=parse_tsv(path)
    assert [r['text'] for r in result]==['ФБС 24.4.6','???']
    assert [r['confidence'] for r in result]==[.4,.3]


def test_tesseract_literal_quote_cannot_consume_following_records(tmp_path):
    path=tmp_path/'literal-quotes.tsv'
    path.write_text('level\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        '5\t1\t1\t1\t10\t10\t20\t20\t90\t"\n'
        '5\t2\t1\t1\t10\t50\t30\t20\t95\tФБС\n'
        '5\t2\t1\t1\t50\t50\t80\t20\t95\t24.4.6\n',encoding='utf8')
    result=parse_tsv(path)
    assert [row['text'] for row in result]==['"','ФБС 24.4.6']
    assert result[1]['bbox']=={'left':10,'top':50,'width':120,'height':20}
    assert all('\t' not in row['text'] and '\n' not in row['text'] for row in result)


def table_line(number,text,x,y,width=80,confidence=.96,page=1):
    return {'line':number,'text':text,'confidence':confidence,'page':page,
            'bbox':{'left':x,'top':y,'width':width,'height':20}}


def test_specification_table_uses_quantity_not_mass_or_drawing_dimensions():
    lines=[table_line(1,'Наименование',100,40,180),table_line(2,'Кол.',420,40,40),
           table_line(3,'ФБС 24.4.6',110,100,170),table_line(4,'218',425,100,30),
           table_line(5,'179995',510,100,70),table_line(6,'ФБС 12.4.6',110,150,170),
           table_line(7,'95',425,150,30),table_line(8,'ФБС 9.4.6',110,200,170),
           table_line(9,'128',425,200,30)]
    rows=candidate_rows(lines)
    assert [(r['name'],r['quantity'],r['unit']) for r in rows]==[
        ('ФБС 24.4.6','218','шт'),('ФБС 12.4.6','95','шт'),('ФБС 9.4.6','128','шт')]
    assert all(not row.get('error') for row in rows)
    # Same coordinates on a different page cannot supply a missing quantity.
    lines[3]['page']=2
    assert candidate_rows(lines)[0]['quantity']==''
    assert candidate_rows(lines)[0]['error']


def test_uncertain_table_cells_are_not_repaired_or_silently_trusted():
    lines=[table_line(1,'Наименование',100,40,180),table_line(2,'Кол.',420,40,40),
           table_line(3,'ФБС 12.4.6',110,100,170),table_line(4,'9я',425,100,30,.4),
           table_line(5,'ФБС 94.6',110,150,170),table_line(6,'128',425,150,30),
           table_line(7,'ФБС 24.4.6',110,200,170,.85),table_line(8,'218',425,200,30)]
    rows=candidate_rows(lines)
    assert rows[0]['quantity']=='' and rows[0]['error']
    assert rows[1]['name']=='ФБС 94.6' and rows[1]['unit']=='' and rows[1]['error']
    assert rows[2]['quantity']=='218' and rows[2]['error']
    no_header=candidate_rows([line for line in lines if line['line']!=2])
    assert all(not row['quantity'] for row in no_header)


def test_full_budget_ocr_is_indexed_once_and_matching_is_bounded(monkeypatch):
    import procurement.pdf_ocr as ocr
    class CountingLines(list):
        iterations = 0
        def __iter__(self):
            self.iterations += 1
            return super().__iter__()
    lines = CountingLines()
    for page in range(1,13):
        lines.extend([table_line(len(lines)+1,'Наименование',100,0,180,page=page),
                      table_line(len(lines)+2,'Кол.',420,0,40,page=page)])
        for row in range(599):
            lines.extend([table_line(len(lines)+1,'ФБС 24.4.6',110,40+row*24,170,page=page),
                          table_line(len(lines)+2,'218',425,40+row*24,30,page=page)])
    assert len(lines)==ocr.MAX_REVIEW_LINES
    calls = []
    original = ocr._table_quantity
    def counted(line,index):
        calls.append(id(index))
        return original(line,index)
    monkeypatch.setattr(ocr,'_table_quantity',counted)
    rows=ocr.candidate_rows(lines)
    assert len(rows)==500
    assert rows[0]['quantity']=='218'
    assert lines.iterations==2  # Index build + bounded candidate traversal only.
    assert len(calls)==500 and len(set(calls))==1
    assert len(lines)==ocr.MAX_REVIEW_LINES  # No raw line silently removed.


def test_native_russian_image_only_pdf_opens_and_source_unchanged(tmp_path):
    if not shutil.which('tesseract') or not shutil.which('pdftoppm'):
        pytest.skip('native OCR binaries required; container/CI test is mandatory')
    assert not PdfReader(FIXTURE).pages[0].extract_text()
    original=FilePayload(FIXTURE);before=payload_sha256(original)
    result=extract_pdf_page_review(original,1)
    assert result['mode']=='ocr' and 'Спецификация' in result['text']
    assert '001230040500' in result['text'] and 'количество уточнить' in result['text']
    assert result['lines'] and payload_sha256(original)==before
    with pytest.raises(ValueError):extract_pdf_page_review(original,2)


def test_pdf_review_requires_all_lines_corrected_items_source_and_owner(workflow):
    db,service,launch=workflow;pr=project(service)
    doc=service.register_source_document(filename='scan.pdf',content=FIXTURE.read_bytes(),document_type='project_section',project_id=pr['id'])
    extracted={'mode':'ocr','lines':[{'line':1,'text':'ФБС 24.4.6 ???','confidence':.65},
                                    {'line':2,'text':'Количество уточнить','confidence':.3}]}
    review=launch.pdf_review(doc,1,extracted)
    assert review['decision']=='human_review_required' and not service.list_lots()
    pid=review['preview']['preview_id'];payload=lot_payload(pr['id'])
    for confirmed,lines in [(False,[1,2]),(True,[1]),(True,[1,1,2])]:
        with pytest.raises(ValueError):launch.create_sheet_lot(pid,payload,confirmed,kind='pdf_ocr',reviewed_line_ids=lines)
        assert not service.list_lots()
    from procurement.identity import authenticated_actor
    token=authenticated_actor.set('other-user')
    try:
        with pytest.raises(Exception,match='другому пользователю'):
            launch.create_sheet_lot(pid,payload,True,kind='pdf_ocr',reviewed_line_ids=[1,2])
    finally:authenticated_actor.reset(token)
    lot=launch.create_sheet_lot(pid,payload,True,kind='pdf_ocr',reviewed_line_ids=[1,2])
    assert lot['items'][0]['quantity']=='10' and lot['items'][0]['source_page']==1
    assert lot['attachments'][0]['sha256']==doc['sha256']
    assert payload_sha256(launch.document_file(doc))==doc['sha256']
    assert launch.create_sheet_lot(pid,payload,True,kind='pdf_ocr',reviewed_line_ids=[1,2])['id']==lot['id']
    assert db.one("SELECT * FROM audit_log WHERE action='lot_created_from_pdf_ocr'")['actor']=='staff-a'


def test_scan_review_inherits_existing_project_cluster_without_migration(workflow):
    db,service,launch=workflow;pr=project(service)
    with db.connection() as conn:conn.execute("UPDATE projects SET cluster='cluster_1' WHERE id=?",(pr['id'],))
    doc=service.register_source_document(filename='scan.pdf',content=FIXTURE.read_bytes(),document_type='project_section',project_id=pr['id'])
    review=launch.pdf_review(doc,1,{'lines':[{'line':1,'text':'ФБС 24.4.6','confidence':.9}]})
    lot=launch.create_sheet_lot(review['preview']['preview_id'],lot_payload(pr['id']),True,kind='pdf_ocr',reviewed_line_ids=[1])
    assert lot['cluster']=='cluster_1' and service.get_project(pr['id'])['cluster']=='cluster_1'


def test_missing_ocr_and_native_timeout_are_honest(monkeypatch,tmp_path):
    from procurement.pdf_ocr import _run
    import subprocess
    def fail(*a,**kw):raise FileNotFoundError()
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(ValueError,match='OCR не установлен'):_run(['tesseract'],1)
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('tesseract',1)
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(ValueError,match='слишком долго'):_run(['tesseract'],1)
