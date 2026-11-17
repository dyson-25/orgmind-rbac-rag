import importlib.util
import sqlite3
import sys

import fitz
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer


def test_python_312_compatible():
    assert sys.version_info >= (3, 12)


def test_lightweight_runtime():
    assert importlib.util.find_spec('torch') is None
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
        conn.execute("INSERT INTO probe VALUES('travel reimbursement')")
        assert conn.execute("SELECT count(*) FROM probe WHERE probe MATCH 'travel'").fetchone()[0] == 1
    finally:
        conn.close()


def test_pdf_and_lsa():
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), 'PDF smoke')
        with fitz.open(stream=doc.tobytes(), filetype='pdf') as reopened:
            assert 'PDF smoke' in reopened[0].get_text()
    matrix = TfidfVectorizer().fit_transform(['car travel mileage', 'cloud service availability', 'travel stipend'])
    assert TruncatedSVD(n_components=2, random_state=42).fit_transform(matrix).shape == (3, 2)
