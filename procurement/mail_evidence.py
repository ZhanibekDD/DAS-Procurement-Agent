"""Read-only SMTP evidence projection; never rewrite history or enable resends."""
import json
from datetime import datetime


def smtp_accepted(row):
    if not row or row.get('status') != 'sent':
        return False
    try:
        recipients = json.loads(row['recipients_json'])
        accepted = json.loads(row['accepted_recipients_json'])
        if not isinstance(recipients, list) or not isinstance(accepted, list):
            return False
        expected = {r.strip().casefold() for r in recipients}
        actual = {r.strip().casefold() for r in accepted}
        timestamp = datetime.fromisoformat(row['accepted_at'])
        return bool(expected and expected == actual
                    and row['recipient'].strip().casefold() in expected
                    and timestamp.tzinfo is not None
                    and row['rfc_message_id'].strip()
                    and isinstance(row['smtp_code'], int)
                    and 200 <= row['smtp_code'] < 300)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def lot_mail_projection(lot, messages):
    counts = dict(total=len(messages), accepted=0, unknown=0, failed=0, queued=0, sending=0, draft=0)
    for row in messages:
        if smtp_accepted(row) and row['message_status'] == 'sent':
            counts['accepted'] += 1
        elif row['message_status'] == 'sent' or row.get('status') in {'sent', 'unknown'}:
            counts['unknown'] += 1
        elif row['message_status'] in {'failed', 'queued', 'sending'}:
            counts[row['message_status']] += 1
        else:
            counts['draft'] += 1
    display = lot['status']
    if display in {'draft', 'rfq_draft', 'rfq_sent'}:
        if counts['accepted']:
            display = 'rfq_sent' if counts['accepted'] == counts['total'] else 'rfq_partial'
        elif counts['unknown'] or display == 'rfq_sent':
            display = 'rfq_unconfirmed'
        elif counts['sending']:
            display = 'rfq_sending'
        elif counts['queued']:
            display = 'rfq_queued'
        elif counts['failed']:
            display = 'rfq_failed'
    return {'display_status': display, 'mail_summary': counts}
