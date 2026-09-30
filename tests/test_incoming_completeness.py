"""An IMAP OK is not sufficient evidence that the entire UID window was read."""
import pytest
from procurement.incoming_mail import Inbox, poll
from test_incoming_mail import Mailbox, mailbox_env, message
from test_launch_workflow import workflow


@pytest.mark.parametrize('kind',['none','subset','missing','extra','duplicate'])
def test_incomplete_fetch_keeps_cursor_then_recovers(workflow,mailbox_env,kind):
    db,s,w=workflow;inbox=Inbox(s,w)
    mb=Mailbox({1:message(),3:message(subject='second quote')});original=mb.uid
    def incomplete(command,ids,fields):
        status,values=original(command,ids,fields)
        if command=='FETCH' and fields=='(UID RFC822.SIZE)':
            values={'none':[None],'subset':values[:1],'missing':[],
                'extra':values+[b'4 (UID 4 RFC822.SIZE 10)'],
                'duplicate':[values[0],values[0]]}[kind]
        return status,values
    mb.uid=incomplete
    for _ in range(3):
        assert poll(inbox,lambda:mb)['status']=='error'
        assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==0
        assert not db.all('SELECT * FROM inbox_messages')
    mb.uid=original
    assert poll(inbox,lambda:mb)=={'status':'ok','received':2,'caught_up':True}
    assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==3
    assert len(db.all('SELECT * FROM inbox_attachments'))==2
    assert poll(inbox,lambda:mb)['received']==0


@pytest.mark.parametrize('values',[[None],[],[b'1',b'3'],[b'1 1'],[b'1 26'],[b'0'],[b'x'],[b'1 '*1000]])
def test_invalid_search_cannot_advance_cursor(workflow,mailbox_env,values):
    db,s,w=workflow;inbox=Inbox(s,w);mb=Mailbox({1:message()})
    mb.uid=lambda *args:('OK',values)
    assert poll(inbox,lambda:mb)['status']=='error'
    assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==0


def test_genuinely_empty_uid_window_is_confirmed_without_fetch(workflow,mailbox_env):
    db,s,w=workflow;inbox=Inbox(s,w);mb=Mailbox({26:message()})
    assert poll(inbox,lambda:mb)=={'status':'ok','received':0,'caught_up':False}
    assert mb.commands==[(None,'UID 1:25')]
    assert poll(inbox,lambda:mb)=={'status':'ok','received':1,'caught_up':True}


def test_message_deleted_between_search_fetch_is_retried_not_silently_skipped(workflow,mailbox_env):
    db,s,w=workflow;inbox=Inbox(s,w);mb=Mailbox({1:message(),3:message(subject='later')});original=mb.uid
    def disappearing(command,ids,fields):
        if command=='FETCH' and fields=='(UID RFC822.SIZE)':mb.messages.pop(1,None)
        return original(command,ids,fields)
    mb.uid=disappearing
    assert poll(inbox,lambda:mb)['status']=='error'
    assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==0
    mb.uid=original
    assert poll(inbox,lambda:mb)=={'status':'ok','received':1,'caught_up':True}
    assert [r['uid'] for r in db.all('SELECT uid FROM inbox_messages')]==[3]
