import pytest
from procurement.models import LotCreate, ProcurementSuggestionCreate, ProcurementSuggestionApproval
from procurement.table_ingest import read_table
from test_launch_workflow import workflow, project, lot_payload, FIXTURES


@pytest.mark.parametrize('kind',['manual','sheet','pdf','suggestion'])
@pytest.mark.parametrize('currency',['RUB','USD','EUR','KZT'])
def test_all_lot_insertions_enforce_rub_atomically(workflow,kind,currency):
    db,service,w=workflow;pr=project(service);data={**lot_payload(pr['id']),'currency':currency}
    if kind=='manual':
        create=lambda:service.create_lot(LotCreate(**data))
    else:
        filename='russian_scan.pdf' if kind=='pdf' else 'items.xlsx'
        content=(FIXTURES/filename).read_bytes()
        doc=service.register_source_document(filename=filename,content=content,document_type='project_section',project_id=pr['id'])
        if kind=='suggestion':
            s=service.register_procurement_suggestions(doc['id'],[ProcurementSuggestionCreate(
                section_code='ТЕСТ',section_name='Тестовый раздел',lot_title=data['title'],items=data['items'],confidence=0.8)])[0]
            create=lambda:service.approve_procurement_suggestion(s['id'],ProcurementSuggestionApproval(
                response_deadline=data['response_deadline'],currency=currency,approved_by='Тест'))['lot']
        elif kind=='sheet':
            p=w.sheet_preview(read_table(content,filename),source_document=doc)
            create=lambda:w.create_sheet_lot(p['preview_id'],data,True)
        else:
            p=w.save_preview('pdf_ocr',{'source_document_id':doc['id'],'source_sha256':doc['sha256'],
                'project_id':pr['id'],'source_page':1,'sheet':'1','lines':[{'line':1,'text':'Тест OCR'}]})
            create=lambda:w.create_sheet_lot(p['preview_id'],data,True,kind='pdf_ocr',reviewed_line_ids=[1])
    tables=('lots','lot_items','lot_attachments','project_sections','source_documents','procurement_suggestions','launch_previews','audit_log')
    before={t:db.all('SELECT * FROM '+t) for t in tables}
    if currency=='RUB':
        lot=create();assert lot['currency']=='RUB' and lot['cluster']==pr['cluster']
    else:
        with pytest.raises(ValueError,match='только в рублях'):
            create()
        assert all(db.all('SELECT * FROM '+t)==rows for t,rows in before.items())
