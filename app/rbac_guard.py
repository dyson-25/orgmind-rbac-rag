SCOPES = ('general', 'hr', 'finance', 'engineering', 'executive')
ROLE_SCOPES = {
    'admin': SCOPES,
    'finance': ('general', 'finance'),
    'hr': ('general', 'hr'),
    'engineering': ('general', 'engineering'),
    'employee': ('general',),
    'executive': ('general', 'executive'),
}
PERSONAS = {
    'admin_user': 'admin', 'finance_user': 'finance', 'hr_user': 'hr',
    'eng_user': 'engineering', 'general_user': 'employee', 'executive_user': 'executive',
}
DENIAL = 'Insufficient authorized evidence in your permitted workspace scopes to answer this request.'


def authorized_scopes(user):
    return ROLE_SCOPES[user['role']]


def validate_scopes(scopes):
    if not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError('Invalid scopes')
    return sorted(set(scopes))


def authorized_document_ids(conn, user):
    if user['role'] == 'admin':
        return {row[0] for row in conn.execute('SELECT id FROM documents')}
    scopes = authorized_scopes(user)
    return {row[0] for row in conn.execute(
        "SELECT d.id FROM documents d WHERE (d.visibility='private' AND d.owner_id=?) "
        "OR (d.visibility='scoped' AND EXISTS (SELECT 1 FROM document_scopes s "
        f'WHERE s.document_id=d.id AND s.scope IN ({",".join("?" for _ in scopes)})))',
        (user['id'], *scopes))}


def authorized_chunk_ids(conn, user):
    allowed = authorized_document_ids(conn, user)
    return {row['id'] for row in conn.execute('SELECT id,document_id FROM chunks')
            if row['document_id'] in allowed}


def validate_citations(citations, allowed):
    if any(c['chunk_id'] not in allowed for c in citations):
        raise ValueError('Citation authorization failed')
