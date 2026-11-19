"""Regenerate the one optional sample PDF from its editable Markdown source."""

from pathlib import Path

import pymupdf


def build():
    folder = Path(__file__).resolve().parent
    sections = (folder / 'personal-finance-guide.md').read_text().split('\n---\n')
    with pymupdf.open() as pdf:
        for number, section in enumerate(sections, 1):
            lines = [line.lstrip('# ').rstrip() for line in section.strip().splitlines()]
            if number == 1:
                lines = lines[2:]
            heading, body = lines[0], '\n'.join(lines[1:]).strip()
            page = pdf.new_page(width=595, height=842)
            page.insert_text((50, 45), 'ORGMIND / OPTIONAL SAMPLE', fontsize=9, color=(0.2, 0.35, 0.4))
            page.insert_textbox(pymupdf.Rect(50, 75, 545, 140), heading, fontsize=20, fontname='hebo')
            remaining = page.insert_textbox(pymupdf.Rect(50, 155, 545, 770), body, fontsize=12, lineheight=1.4)
            if remaining < 0:
                raise ValueError(f'Sample page {number} overflows; shorten the source text')
            page.insert_text((50, 805), f'Personal Finance and Budgeting Guide | Page {number}', fontsize=9)
        pdf.set_metadata({'title': 'Personal Finance and Budgeting Guide', 'author': 'OrgMind', 'subject': 'Original educational PDF sample'})
        pdf.save(folder / 'personal-finance-guide.pdf', garbage=4, deflate=True, no_new_id=True)


if __name__ == '__main__':
    build()
