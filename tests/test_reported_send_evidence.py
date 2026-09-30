import pytest
from procurement.mail_evidence import smtp_accepted,lot_mail_projection
from procurement.mail_delivery import journal
from test_launch_workflow import workflow
from test_launch_mail import prepare,smtp_env
from smtp_capture import CaptureSMTP

PROOF=dict(status='sent',message_status='sent',recipient='supplier@example.test',recipients_json='["supplier@example.test"]',
           accepted_recipients_json='["supplier@example.test"]',accepted_at='2026-09-30T00:00:00+00:00',
           smtp_code=250,rfc_message_id='<test@example.test>')

@pytest.mark.parametrize('change',[{}, {'accepted_at':None},{'accepted_at':'bad'}, {'smtp_code':550},
    {'rfc_message_id':''},{'accepted_recipients_json':'[]'},{'recipients_json':'[]'},
    {'accepted_recipients_json':'["other@example.test"]'},{'recipients_json':'null'},
    {'accepted_recipients_json':'[null]'}, {'recipient':'different@example.test'}])
def test_only_full_smtp_proof_means_sent(change):
    row={**PROOF,**change}
    assert smtp_accepted(row) is (not change)
    projected=lot_mail_projection({'status':'rfq_sent'},[row])
    assert projected['display_status']==('rfq_sent' if not change else 'rfq_unconfirmed')

def test_partial_send_is_not_complete_and_later_business_stage_is_preserved():
    assert lot_mail_projection({'status':'rfq_sent'},[PROOF,{'message_status':'failed'}])['display_status']=='rfq_partial'
    assert lot_mail_projection({'status':'rfq_sent'},[])['display_status']=='rfq_unconfirmed'
    assert lot_mail_projection({'status':'ordered'},[])['display_status']=='ordered'
    for state in ('failed','queued','sending'):
        assert lot_mail_projection({'status':'rfq_draft'},[{'message_status':state}])['display_status']=='rfq_'+state

def test_old_lot_message_and_audit_survive_read_only_projection(workflow):
    db,service,launch=workflow;message,_=prepare(workflow)
    lid=db.one('SELECT lot_id FROM campaigns WHERE id=?',(message['campaign_id'],))['lot_id']
    with db.connection() as conn:
        conn.execute("UPDATE outbox_messages SET status='sent' WHERE id=?",(message['id'],))
        conn.execute("UPDATE lots SET status='rfq_sent' WHERE id=?",(lid,))
    before={t:db.all('SELECT * FROM '+t) for t in ('lots','outbox_messages','audit_log')}
    for lot in (service.get_lot(lid),next(l for l in service.list_lots() if l['id']==lid)):
        assert lot['status']=='rfq_sent' and lot['display_status']=='rfq_unconfirmed'
        assert lot['mail_summary']['accepted']==0 and lot['mail_summary']['unknown']==1
    assert all(db.all('SELECT * FROM '+t)==rows for t,rows in before.items())

def test_real_smtp_confirmation_updates_projection_but_missing_proof_does_not(workflow,monkeypatch):
    db,service,launch=workflow;message,_=prepare(workflow)
    lid=db.one('SELECT lot_id FROM campaigns WHERE id=?',(message['campaign_id'],))['lot_id']
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp);launch.send(message['id'],True)
        assert service.get_lot(lid)['display_status']=='rfq_sent'
        assert service.get_lot(lid)['mail_summary']['accepted']==1
        with db.connection() as conn:
            conn.execute("UPDATE mail_receipts SET accepted_recipients_json='[]' WHERE message_id=?",(message['id'],))
        assert service.get_lot(lid)['display_status']=='rfq_unconfirmed'
        assert journal(db,message['id'])['status']=='unknown'
        assert not journal(db,message['id'])['retry_allowed']
        assert not launch.send(message['id'],True)['accepted_by_smtp']
        assert len(smtp.messages)==1

def test_activity_does_not_claim_old_unproved_mail_sent(workflow,monkeypatch):
    import procurement.app as app
    db,service,launch=workflow;message,_=prepare(workflow)
    monkeypatch.setattr(app,'service',service)
    db.audit('mail_sent','outbox_message',message['id'])
    assert app.recent_activity(limit=8)[0]['label']=='Отправка запроса КП не подтверждена'
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp);launch.send(message['id'],True)
    assert app.recent_activity(limit=8)[0]['label']=='Запрос КП отправлен'
