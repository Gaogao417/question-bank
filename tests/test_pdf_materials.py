"""PDF preparation keeps full evidence and the existing no-crop boundary."""
import shutil

import pytest

from question_bank.agent_tools import (
    ToolError, build_package, new_task, prepare_pdf, read_json, write_json,
)


def blank_pdf(path):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 144 216] >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 144 216] >>']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 5\n0000000000 65535 f \n'
    data += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    data += f'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
    path.write_bytes(data)


@pytest.mark.skipif(not shutil.which('pdftoppm'), reason='Poppler not installed')
def test_pdf_evidence_separate_sources_and_change_protection(tmp_path):
    pdf = tmp_path / 'original.pdf'
    blank_pdf(pdf)
    original = pdf.read_bytes()
    out = tmp_path / 'materials'
    result = prepare_pdf([pdf, pdf], 'PDF-TEST', out, dpi=72)
    assert result['pages'] == 4
    m = read_json(result['materials'])
    assert m['illustrations'] == {}
    assert m['pages']['src2-p001']['global_page_number'] == 3
    assert pdf.read_bytes() == original
    task = tmp_path / 'Q001'
    new_task(task, 1, 'fillin', 4)
    (task / 'stem.md').write_text('求值。')
    meta = read_json(task / 'meta.json')
    meta['question_pages'] = ['src1-p001', 'src1-p002']
    write_json(task / 'meta.json', meta)
    payload = build_package(out / 'materials.json', task, tmp_path / 'package')
    assert len(payload['evidence']) == 2
    assert payload['images'] == []
    pdf.write_bytes(original + b'\nchanged')
    with pytest.raises(ToolError, match='Source changed'):
        build_package(out / 'materials.json', task, tmp_path / 'changed')
    with pytest.raises(ToolError, match='already exists'):
        prepare_pdf([pdf], 'PDF-TEST', out)


def test_pdf_rejects_invalid_source_without_creating_output(tmp_path):
    out = tmp_path / 'materials'
    with pytest.raises(ToolError):
        prepare_pdf([tmp_path / 'missing.pdf'], 'PDF-TEST', out)
    assert not out.exists()
