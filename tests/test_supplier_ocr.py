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


def test_tsv_preserves_all_lines_and_marks_uncertainty(tmp_path):
    path=tmp_path/'scan.tsv'
    path.write_text('level\tblock_num\tpar_num\tline_num\tleft\ttop\theight\tconf\ttext\n'
                    '5\t1\t1\t1\t10\t10\t20\t90\tФБС\n'
                    '5\t1\t1\t1\t50\t10\t20\t40\t24.4.6\n'
                    '5\t2\t1\t1\t10\t50\t20\t30\t???\n',encoding='utf8')
    result=parse_tsv(path)
    assert [r['text'] for r in result]==['ФБС 24.4.6','???']
    assert [r['confidence'] for r in result]==[.4,.3]


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


def test_missing_ocr_and_native_timeout_are_honest(monkeypatch,tmp_path):
    from procurement.pdf_ocr import _run
    import subprocess
    def fail(*a,**kw):raise FileNotFoundError()
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(ValueError,match='OCR не установлен'):_run(['tesseract'],1)
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('tesseract',1)
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(ValueError,match='слишком долго'):_run(['tesseract'],1)
