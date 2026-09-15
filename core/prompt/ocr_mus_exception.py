"""OCR prompt untuk dokumen konfirmasi pengecualian MUS (11 September 2026).

Lihat docs/csv_bank/11 September 2026/MUS_logic_update.md. Dokumennya screenshot
email balasan Bank Mega yang mengonfirmasi sebuah tiket memang eligible Mega
Cashline TANPA Mega Ultima Shield (penyakit termasuk
``compliance.mus_exemption.DISEASE_WHITELIST``), contoh acuan: tiket
``030808fLO1``, subject "Re: Campaign Cashline Tidak Eligible MUS - 030808fLO1",
body "Oke confirm, please proceed."

Skema ini SENGAJA bukan pola ``verifications`` dokumen lain (``_common.py``) —
tidak ada nilai ACUAN bank untuk dibandingkan. Ekstraksinya cuma dua field, dan
pencocokan ``ticket_id_found`` terhadap tiket yang sedang dinilai dilakukan di
Python (``compliance.documents.mus_exception_doc_confirmed``), BUKAN oleh LLM —
supaya model tidak sekadar menggemakan ticket ID yang "diharapkan" alih-alih
membaca dokumennya.
"""

REQUIRED = ["ticket_id_found", "approval_statement_present"]

PROPS = {
    "ticket_id_found": {
        "type": ["string", "null"],
        "description": (
            "Ticket ID yang disebut pada subject atau body email, disalin apa "
            "adanya. null bila tidak ada ticket ID yang disebut sama sekali."
        ),
    },
    "approval_statement_present": {
        "type": "boolean",
        "description": (
            "true bila email memuat kalimat persetujuan/konfirmasi dari Bank Mega "
            "untuk memproses Mega Cashline TANPA Mega Ultima Shield (misal 'oke "
            "confirm, please proceed', 'disetujui', 'silakan diproses') — dinilai "
            "dari MAKNA kalimat, bukan kecocokan kata per kata."
        ),
    },
}

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "mus_exception_confirmation",
        "schema": {
            "type": "object",
            "properties": PROPS,
            "required": REQUIRED,
            "additionalProperties": False,
        },
        "strict": True,
    },
}

PROMPT = (
    "Anda membaca screenshot email terkait pengecualian Mega Ultima Shield (MUS) "
    "dari Bank Mega. Email ini mengonfirmasi apakah sebuah tiket Mega Cashline "
    "boleh diproses TANPA Mega Ultima Shield karena alasan kesehatan nasabah.\n\n"
    "TUGAS:\n"
    "1. Cari TICKET ID yang disebut pada SUBJECT atau BODY email (mis. kode "
    "seperti '030808fLO1'). Salin apa adanya ke 'ticket_id_found'; null bila tidak "
    "ada ticket ID yang disebut sama sekali. JANGAN menduga-duga dari konteks "
    "lain.\n"
    "2. Tentukan apakah email memuat kalimat PERSETUJUAN/KONFIRMASI dari Bank Mega "
    "untuk memproses tiket tersebut TANPA MUS (misal 'oke confirm, please "
    "proceed', 'disetujui', 'silakan diproses') — nilai berdasarkan MAKNA kalimat, "
    "bukan kecocokan kata tunggal. Tulis true/false ke "
    "'approval_statement_present'.\n\n"
    "OUTPUT: satu objek JSON dengan key 'ticket_id_found' dan "
    "'approval_statement_present'."
)


def build_prompt(reference: dict) -> str:
    """Tidak ada acuan bank untuk dokumen ini — ``reference`` diterima supaya
    polanya tetap konsisten dengan modul OCR lain (``build_ocr_request`` memanggil
    ``module.build_prompt(reference)`` secara generik), tapi tidak dipakai."""
    return PROMPT
