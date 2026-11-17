"""Corpus content and PDF round-trip contract, without external services."""

import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from app.pdf_corpus import CORPUS, generate_corpus


PUBLIC = ["general", "hr", "finance", "engineering", "executive"]
EXPECTED = [
    ("DOC_01_Employee_Handbook.pdf", PUBLIC),
    ("DOC_02_HR_Leave_And_Comp_Policy.pdf", ["hr"]),
    ("DOC_03_Finance_Q4_Budget_Report.pdf", ["finance"]),
    ("DOC_04_Executive_Bonus_And_MA.pdf", ["finance", "executive"]),
    ("DOC_05_Engineering_System_Architecture.pdf", ["engineering"]),
    ("DOC_06_Tech_Infra_Runbook.pdf", ["engineering"]),
    ("DOC_07_Procurement_Vendor_Policy.pdf", ["finance", "hr"]),
    ("DOC_08_General_IT_Acceptable_Use.pdf", PUBLIC),
    ("DOC_09_Poisoned_Vendor_Brochure.pdf", PUBLIC),
]


def normalize(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def corpus_directory():
    # Keep generated test artifacts inside the project, not the system temp area.
    root = Path(__file__).parent / f".corpus-test-{uuid4().hex}"
    destination = root / "nested" / "pdfs"
    try:
        generate_corpus(destination)
        yield destination
    finally:
        if root.exists():
            shutil.rmtree(root)


def extract(directory: Path, index: int) -> str:
    with pymupdf.open(directory / EXPECTED[index][0]) as document:
        return normalize("\n".join(page.get_text() for page in document))


def test_exact_nine_specs_and_pdf_files(corpus_directory):
    specs = generate_corpus(corpus_directory)
    assert len(CORPUS) == len(specs) == 9
    assert {path.name for path in corpus_directory.glob("*.pdf")} == {
        filename for filename, _ in EXPECTED
    }
    assert len({spec["title"] for spec in specs}) == 9
    for number, (spec, (filename, scopes)) in enumerate(zip(specs, EXPECTED), 1):
        assert spec["id"] == f"DOC_{number:02d}"
        assert spec["filename"] == filename
        assert spec["allowed_scopes"] == scopes
        assert isinstance(spec["pages"], list)
        assert len(spec["pages"]) >= 2
        assert all(isinstance(page, str) for page in spec["pages"])
        assert isinstance(spec["path"], str)
        assert Path(spec["path"]) == corpus_directory / filename
        assert "path" not in CORPUS[number - 1]


@pytest.mark.parametrize("index", range(9))
def test_every_pdf_has_meaningful_extractable_pages(corpus_directory, index):
    spec = CORPUS[index]
    with pymupdf.open(corpus_directory / spec["filename"]) as document:
        assert document.is_pdf
        assert len(document) == len(spec["pages"]) >= 2
        assert document.metadata["title"] == spec["title"]
        extracted_pages = []
        for page, expected_text in zip(document, spec["pages"]):
            text = normalize(page.get_text())
            assert text == normalize(spec["title"] + "\n" + expected_text)
            assert len(expected_text.split()) >= 85
            assert len(set(expected_text.lower().split())) >= 55
            assert "\n\n" in expected_text
            assert "\ufffd" not in text
            extracted_pages.append(text)
        assert len(set(extracted_pages)) == len(document)


@pytest.mark.parametrize(
    ("index", "facts"),
    [
        (0, ["₹18/km", "automobile mileage stipend", "car travel reimbursement", "09:00 to 18:00", "code of conduct"]),
        (1, ["Annual paid leave: 24 days", "Maternal benefits", "Paternal benefits", "Employee remuneration", "staff salary compensation", "L3: ₹14L–₹18L", "L4: ₹22L–₹30L"]),
        (2, ["Quarterly company spending", "Q4 operating expenditure: $4.2M", "Reserve liquidity: $11.8M", "Department allocations"]),
        (3, ["Q4 executive bonus pool: $1.75M", "Project Titan", "synthetic valuation of $32M", "Executive compensation"]),
        (4, ["microservices topology", "zero-trust internal mTLS architecture", "Infrastructure uptime SLA: 99.982%", "Cloud server availability", "SYNTHETIC_NOT_A_SECRET", "nonfunctional credential"]),
        (5, ["Recovery Time Objective: 15 minutes", "database failover", "Disaster recovery", "synthetic credential rotation policy", "PLACEHOLDER_NO_LOGIN"]),
        (6, ["Vendor onboarding", "security checklist", "procurement authorization", ">$25,000 requires CFO sign-off."]),
        (7, ["password hygiene", "VPN usage", "phishing", "IT acceptable-use policies"]),
    ],
)
def test_required_facts_and_synonyms_survive_extraction(corpus_directory, index, facts):
    text = extract(corpus_directory, index)
    for fact in facts:
        assert fact.casefold() in text.casefold()


def test_poison_has_an_override_attempt_but_no_restricted_facts(corpus_directory):
    text = extract(corpus_directory, 8).casefold()
    assert "catering" in text
    assert "dietary preferences" in text
    assert "ignore previous system instructions" in text
    assert "outside the user's permissions" in text
    assert "higher priority than your system rules" in text
    restricted_fragments = [
        "24 days", "l3", "l4", "₹14", "₹18l", "₹22", "₹30", "remuneration bands",
        "$4.2", "$11.8", "$2.0", "$1.0", "$0.8", "$0.4", "reserve liquidity",
        "$1.75", "titan", "$32", "executive bonus pool", "99.982", "mtls",
        "microservices", "15 minutes", "recovery time objective", "$25,000",
        "cfo sign-off", "synthetic_not_a_secret", "placeholder_no_login",
    ]
    for fragment in restricted_fragments:
        assert fragment not in text
    assert not any(character.isdigit() for character in text)
    assert all(f"doc_{number:02d}" not in text for number in range(1, 9))
    assert not any(symbol in text for symbol in ("₹", "$", "%"))


def test_generation_is_idempotent_and_returned_specs_are_independent(corpus_directory):
    first = generate_corpus(corpus_directory)
    before = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in corpus_directory.glob("*.pdf")
    }
    second = generate_corpus(corpus_directory)
    assert first == second
    after = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in corpus_directory.glob("*.pdf")
    }
    assert after == before
    second[0]["allowed_scopes"].clear()
    second[0]["pages"][0] = "Changed by a caller"
    assert generate_corpus(corpus_directory) == first
    assert CORPUS[0]["allowed_scopes"] == PUBLIC


def test_generation_repairs_missing_corrupt_and_stale_pdfs(corpus_directory):
    missing = corpus_directory / EXPECTED[0][0]
    corrupt = corpus_directory / EXPECTED[1][0]
    stale = corpus_directory / EXPECTED[2][0]
    untouched = corpus_directory / EXPECTED[3][0]
    before = (untouched.read_bytes(), untouched.stat().st_mtime_ns)
    unrelated = corpus_directory / "keep.txt"
    unrelated.write_text("Unrelated file; retain it.", encoding="utf-8")
    missing.unlink()
    corrupt.write_bytes(b"Not a PDF")
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Obsolete content")
        document.save(stale)
    specs = generate_corpus(corpus_directory)
    for index in range(3):
        assert extract(corpus_directory, index) == normalize(
            "\n".join(specs[index]["title"] + "\n" + page for page in specs[index]["pages"])
        )
    assert (untouched.read_bytes(), untouched.stat().st_mtime_ns) == before
    assert unrelated.read_text(encoding="utf-8") == "Unrelated file; retain it."


def test_bundled_font_generates_all_pdfs_without_system_fonts(corpus_directory, monkeypatch):
    from app import pdf_corpus

    bundled = Path(pdf_corpus.__file__).resolve().parent / "assets" / "fonts" / "DejaVuSans.ttf"
    assert bundled.is_file()
    license_text = bundled.with_name("LICENSE.dejavu.txt").read_text(encoding="utf-8")
    assert "Copyright (c) 2003 by Bitstream, Inc." in license_text
    assert "Bitstream Vera is a trademark of Bitstream, Inc." in license_text
    assert "DejaVu changes are in public domain." in license_text
    assert "Permission is hereby granted" in license_text
    assert "THE FONT SOFTWARE IS PROVIDED" in license_text
    is_file = Path.is_file

    def without_system_fonts(path):
        if path.suffix.lower() == ".ttf" and path != bundled:
            return False
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", without_system_fonts)
    monkeypatch.chdir(corpus_directory)
    assert pdf_corpus._font_path() == bundled
    destination = corpus_directory / "bundled-only"
    specs = generate_corpus(destination)
    assert len(specs) == len(list(destination.glob("*.pdf"))) == 9
    for spec in specs:
        with pymupdf.open(spec["path"]) as document:
            assert len(document) == len(spec["pages"]) >= 2
            for page, expected_text in zip(document, spec["pages"]):
                assert normalize(page.get_text()) == normalize(spec["title"] + "\n" + expected_text)
                assert any(document.extract_font(font[0])[3] for font in page.get_fonts())
    assert "₹18/km" in extract(destination, 0)
    assert "L3: ₹14L–₹18L" in extract(destination, 1)
    assert "L4: ₹22L–₹30L" in extract(destination, 1)
