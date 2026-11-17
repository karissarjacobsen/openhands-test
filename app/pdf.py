"""Minimal, dependency-free PDF writer.

Used to generate sample agreement PDFs. Each generated document contains the
`/sn1/` anchor string so Docusign can auto-place a signature tab when the
document is uploaded and sent.
"""

from __future__ import annotations

from datetime import date


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, width: int = 88) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def build_pdf(title: str, sections: list[tuple[str, str]], footer: str = "") -> bytes:
    """Build a simple multi-line text PDF.

    sections: list of (heading, body) tuples rendered in order.
    """
    # (font, size, leading, text)
    ops: list[tuple[str, int, int, str]] = []
    ops.append(("F2", 18, 26, title))
    ops.append(("F1", 10, 16, f"Generated {date.today().isoformat()}"))
    ops.append(("F1", 10, 16, ""))
    for heading, body in sections:
        ops.append(("F2", 12, 20, heading))
        for line in _wrap(body):
            ops.append(("F1", 10, 15, line))
        ops.append(("F1", 10, 15, ""))
    ops.append(("F2", 12, 20, "Signatures"))
    ops.append(("F1", 10, 15, "Signer signature: /sn1/"))
    ops.append(("F1", 10, 15, "Date: ______________________"))
    if footer:
        ops.append(("F1", 8, 12, ""))
        ops.append(("F1", 8, 12, footer))

    content_parts = ["BT", "50 760 Td", "0 -0 Td"]
    first = True
    for font, size, leading, text in ops:
        if not first:
            content_parts.append(f"0 -{leading} Td")
        first = False
        content_parts.append(f"/{font} {size} Tf")
        content_parts.append(f"({_escape(text)}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1", "replace")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(out)
