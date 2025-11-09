"""Parse the Sehat Card mock-data PDF into structured JSON output.

This script performs minimal PDF text extraction without external
libraries by decoding stream objects and applying embedded ToUnicode
CMaps. It then parses the hospital facilities, citizen profiles, and
Sehat Card rules into structured data and prints each section as JSON.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
import zlib

PDF_PATH = Path("Mock Data for Sehat Card Agent.pdf")


@dataclass
class PDFTextExtractor:
    """Simple PDF text extractor that honours ToUnicode CMaps."""

    pdf_bytes: bytes

    STREAM_PATTERN = re.compile(
        rb"(\d+) 0 obj\s*(<<.*?>>)?\s*stream\r?\n(.*?)\r?\nendstream\s*endobj",
        re.S,
    )

    def _parse_tounicode(self, data: bytes) -> Dict[int, str]:
        mapping: Dict[int, str] = {}
        for line in data.decode("latin1").splitlines():
            line = line.strip()
            if not line.startswith("<"):
                continue
            parts = line.split()
            if len(parts) < 2 or not (parts[0].startswith("<") and parts[1].startswith("<")):
                continue
            src_hex = parts[0][1:-1]
            dst_hex = parts[1][1:-1]
            try:
                src_code = int(src_hex, 16)
            except ValueError:
                continue
            text = ""
            if len(dst_hex) % 4 == 0 and dst_hex:
                for i in range(0, len(dst_hex), 4):
                    try:
                        text += chr(int(dst_hex[i : i + 4], 16))
                    except ValueError:
                        pass
            else:
                try:
                    text = bytes.fromhex(dst_hex).decode("utf-16-be")
                except Exception:
                    try:
                        text = bytes.fromhex(dst_hex).decode("utf-8")
                    except Exception:
                        text = ""
            if text:
                mapping[src_code] = text
        return mapping

    def _decode_hex_string(self, hex_bytes: bytes, maps: Sequence[Dict[int, str]]) -> str:
        clean = re.sub(rb"\s+", b"", hex_bytes)
        chars: List[str] = []
        for i in range(0, len(clean), 4):
            chunk = clean[i : i + 4]
            if not chunk:
                continue
            try:
                code = int(chunk, 16)
            except ValueError:
                continue
            mapped = None
            for cmap in maps:
                mapped = cmap.get(code)
                if mapped is not None:
                    break
            if mapped is None:
                mapped = chr(code)
            chars.append(mapped)
        return "".join(chars)

    def _decode_literal(self, literal: bytes) -> str:
        content = literal[1:-1]
        result = bytearray()
        escape = False
        for b in content:
            if escape:
                result.append(b)
                escape = False
            elif b == 0x5C:
                escape = True
            else:
                result.append(b)
        try:
            return result.decode("utf-8")
        except UnicodeDecodeError:
            return result.decode("latin1", errors="ignore")

    def extract(self) -> str:
        cmap_list: List[Dict[int, str]] = []
        for match in self.STREAM_PATTERN.finditer(self.pdf_bytes):
            try:
                stream_data = zlib.decompress(match.group(3))
            except Exception:
                continue
            if b"beginbfchar" in stream_data:
                cmap_list.append(self._parse_tounicode(stream_data))

        segments: List[str] = []
        for match in self.STREAM_PATTERN.finditer(self.pdf_bytes):
            try:
                stream_data = zlib.decompress(match.group(3))
            except Exception:
                continue
            if b"TJ" not in stream_data and b"Tj" not in stream_data:
                continue

            for array_match in re.finditer(rb"\[(.*?)\]\s*TJ", stream_data, re.S):
                tokens = re.findall(rb"<([0-9A-Fa-f\s]+)>|\(([^()]*)\)", array_match.group(1))
                parts: List[str] = []
                for hex_part, literal_part in tokens:
                    if hex_part:
                        parts.append(self._decode_hex_string(hex_part, cmap_list))
                    elif literal_part:
                        literal = b"(" + literal_part + b")"
                        parts.append(self._decode_literal(literal))
                text = "".join(parts).strip()
                if text:
                    segments.append(text)

            for literal_match in re.finditer(rb"(<[0-9A-Fa-f\s]+>|\([^()]*\))\s*Tj", stream_data):
                token = literal_match.group(1)
                if token.startswith(b"<"):
                    text = self._decode_hex_string(token[1:-1], cmap_list)
                else:
                    text = self._decode_literal(token)
                text = text.strip()
                if text:
                    segments.append(text)

        return "\n".join(segments)

def normalise_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_sections(full_text: str) -> Tuple[str, str, str]:
    facilities_marker = "Hospital Facilities (Major Cities)"
    citizens_marker = "Example User Profiles (Sehat Card Data)"
    rules_marker = "Note:"

    if facilities_marker not in full_text or citizens_marker not in full_text:
        raise ValueError("Required section headings were not found in the PDF text.")

    facilities_section = full_text.split(facilities_marker, 1)[1]
    facilities_section, remainder = facilities_section.split(citizens_marker, 1)

    citizens_section = remainder
    rules_section = ""
    if rules_marker in remainder:
        citizens_section, rules_section = remainder.split(rules_marker, 1)

    return facilities_section, citizens_section, rules_section


def parse_facilities(section_text: str) -> List[Dict[str, str]]:
    cities = ["Karachi", "Lahore", "Islamabad", "Peshawar", "Quetta"]
    records: List[Dict[str, str]] = []
    lines = [line.strip() for line in section_text.splitlines() if line.strip()]

    current_city = None
    for line in lines:
        if line in cities:
            current_city = line
            continue
        if "\u2022" in line or not current_city:
            continue
        if " – " not in line:
            continue
        name_part, address_part = [part.strip(" .") for part in line.split(" – ", 1)]
        urdu_name = None
        if "(" in name_part and name_part.endswith(")"):
            base, _, extra = name_part.partition("(")
            urdu_name = extra.rstrip(") ")
            name_part = base.strip()
        facility_type = "Hospital"
        if any(keyword in name_part for keyword in ["Medical Centre", "Medical Complex"]):
            facility_type = "Medical Centre" if "Centre" in name_part else "Medical Complex"
        elif "Hospital" not in name_part:
            facility_type = "Facility"
        facility_id = f"{current_city.lower().replace(' ', '_')}_{re.sub(r'[^a-z0-9]+', '_', name_part.lower()).strip('_')}"
        record = {
            "id": facility_id,
            "name": name_part,
            "type": facility_type,
            "city": current_city,
            "address": address_part,
        }
        if urdu_name:
            record["urdu_name"] = urdu_name
        records.append(record)
    return records

def parse_citizens(section_text: str) -> List[Dict[str, str]]:
    cleaned = section_text.replace("<br>", "<br>\n").replace("\n:\n", ": ")
    pattern = re.compile(
        r"Name\s*:\s*(.*?)<br>\s*Father's Name\s*:\s*(.*?)<br>\s*CNIC\s*:\s*([0-9\-]+)<br>\s*City\s*:\s*(.*?)<br>\s*Income Group\s*:\s*(.*?)<br>\s*Eligibility Status\s*:\s*(.*?)<br>\s*Past Diseases\s*:\s*(.*?)<br>\s*Remaining Credits\s*:\s*(.*?)(?:<br>|\s)(?=Name\s*:|$)",
        re.S,
    )
    citizens: List[Dict[str, str]] = []
    for match in pattern.finditer(cleaned):
        (name, father, cnic, city, income, eligibility, diseases, credits) = (
            normalise_whitespace(val) for val in match.groups()
        )
        citizens.append(
            {
                "name": name,
                "father_name": father,
                "cnic": cnic,
                "city": city,
                "income_group": income,
                "eligibility_status": eligibility,
                "past_diseases": diseases,
                "remaining_credits": credits,
            }
        )
    return citizens

def parse_rules(section_text: str) -> List[Dict[str, str]]:
    section_text = section_text.strip()
    if not section_text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", normalise_whitespace(section_text))
    rules = []
    for idx, sentence in enumerate(sentences, start=1):
        cleaned = sentence.strip("• ")
        if not cleaned or cleaned.isdigit():
            continue
        rules.append({"id": f"rule_{idx}", "description": cleaned})
    return rules


def main(argv: Sequence[str]) -> int:
    if not PDF_PATH.exists():
        print(f"PDF file not found at {PDF_PATH}", file=sys.stderr)
        return 1

    extractor = PDFTextExtractor(pdf_bytes=PDF_PATH.read_bytes())
    full_text = extractor.extract()

    facilities_section, citizens_section, rules_section = extract_sections(full_text)
    facilities = parse_facilities(facilities_section)
    citizens = parse_citizens(citizens_section)
    rules = parse_rules(rules_section)

    print(json.dumps({"citizens": citizens}, ensure_ascii=False, indent=2))
    print(json.dumps({"facilities": facilities}, ensure_ascii=False, indent=2))
    print(json.dumps({"sehat_card_rules": rules}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
