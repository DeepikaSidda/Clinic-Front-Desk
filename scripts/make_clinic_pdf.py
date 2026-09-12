"""Generate a clinic-information PDF to upload in the doctor portal.

Edit :data:`PAGES` and re-run to produce ``clinic-info.pdf``, then upload it at
``/documents?role=doctor``.

The PDF is written by hand rather than with a PDF library because none is a
dependency of this project, and the file needs to be a genuine text-based PDF: a
scan or an image export has no extractable text and the portal will reject it.

Two things about the content matter more than they look:

**Use short headings.** Chunking breaks at section headings, and one topic per
chunk is what makes retrieval work — a passage covering three subjects matches
no question well. A heading here is a line under 45 characters with no comma and
no full stop.

**Keep service names to what a caller would actually say.** The agent matches a
requested service against the configured list by *exact name*, which is the check
that stops it inferring a service from a symptom. "Hearing Test" is matchable;
"Pure Tone Audiometry (PTA) with Impedance" is not, in a phone call.
"""

from __future__ import annotations

from pathlib import Path

OUTPUT = Path("clinic-info.pdf")

PAGES: list[list[str]] = [
    [
        "Aster Narayanadri Hospital - ENT Clinic",
        "Dr. Kuppam Divya Raana",
        "",
        "About the consultant",
        "Dr. Kuppam Divya Raana is an ENT and Otorhinolaryngologist, also",
        "called an ear nose and throat doctor or ENT surgeon. Her",
        "qualifications are MBBS and MS in ENT. She has 12 years of",
        "experience overall and 5 years as a specialist. She completed",
        "MBBS at Meenakshi Medical College and Research Institute,",
        "Enathur in 2014, and MS in ENT at Narayana Medical College,",
        "Nellore in 2021. Her medical registration is verified.",
        "",
        "Patients we treat",
        "The clinic treats both adults and children, including babies and",
        "infants, for ear nose and throat problems. Family members are",
        "welcome to come with the patient.",
        "",
        "About the hospital",
        "Aster Narayanadri Hospital, Tirupati is a 150-bed multispeciality",
        "hospital and part of DM Healthcare's group of patient-centric",
        "hospitals. The ENT clinic runs within the hospital.",
        "",
        "Where to find us",
        "The address is Aster Narayanadri Hospital, ENT Clinic, S Number",
        "73/1A, Renigunta Road, Srinivasa Nagar, Tirupati. The landmark",
        "to look for is Vartha Press: the hospital is beside Vartha Press",
        "on Renigunta Road. Ask for the ENT outpatient desk when you",
        "arrive at the hospital.",
        "",
        "Directions and map",
        "Coming along Renigunta Road, look for Vartha Press; the hospital",
        "is immediately beside it. Tell an auto or taxi driver Aster",
        "Narayanadri Hospital on Renigunta Road, near Vartha Press.",
        "The map location is 13.62849 degrees north, 79.46382 degrees",
        "east, and searching Google Maps for Aster Narayanadri Hospital",
        "Tirupati will find it. If a caller wants the map link sent to",
        "their phone, reception can text or WhatsApp it to them: take the",
        "caller's number and pass the request to reception.",
        "",
        "Clinic timings",
        "The ENT clinic is open Monday to Saturday. The hospital listing",
        "shows the clinic as available from 12:00 AM to 11:59 PM, which",
        "reflects round-the-clock hospital cover rather than the",
        "consultant's own sitting hours. Please confirm Dr. Raana's",
        "consulting hours with reception when you book.",
    ],
    [
        "ENT services offered",
        "The clinic sees adults and children for ear, nose, throat, and",
        "head and neck concerns. The services listed below are the ones",
        "you can ask for by name when booking.",
        "",
        "Consultations",
        "We offer ENT Consultation, Follow-up Consultation and Second",
        "Opinion Consultation.",
        "",
        "Ear services",
        "We offer Ear Examination, Ear Wax Removal for blocked ears,",
        "Hearing Test for hearing loss, Tympanometry, Ear Discharge",
        "Treatment, Grommet Insertion, Tympanoplasty to repair the ear",
        "drum, and Mastoidectomy. We also see ear pain and ear infection.",
        "",
        "Nose and sinus services",
        "We offer Nasal Endoscopy, Sinus Treatment for sinusitis,",
        "Septoplasty to straighten the nose, Sinus Surgery, Nasal Polyp",
        "Removal, Nose Bleed Treatment, and Allergy Testing. We also see",
        "a blocked nose and a runny nose.",
    ],
    [
        "Throat and voice services",
        "We offer Throat Examination, Laryngoscopy, Tonsillectomy to",
        "remove the tonsils, Adenoidectomy to remove the adenoids, Voice",
        "and Hoarseness Assessment for voice change, and Snoring and",
        "Sleep Apnoea Assessment for snoring. We also see sore throat and",
        "difficulty swallowing.",
        "",
        "Head and neck services",
        "We offer Neck Swelling Assessment for a lump in the neck,",
        "Thyroid Swelling Assessment, and Head and Neck Screening.",
        "",
        "Other services",
        "We offer Vertigo and Balance Assessment for dizziness and",
        "giddiness, Foreign Body Removal for something stuck in the ear",
        "nose or throat, and Speech Therapy Referral.",
        "",
        "Fees",
        "Consultation and procedure fees are not listed in this document.",
        "Please ask reception for current charges when you book.",
        "",
        "What to bring and when to arrive",
        "New patients should arrive fifteen minutes early so registration",
        "can be completed before the consultation time. Please bring a",
        "photo ID, your insurance card or scheme details, any previous ENT",
        "reports or scans, and a list of the medicines you are currently",
        "taking. If you have had a hearing test elsewhere, bring the",
        "audiogram with you.",
        "",
        "For a hearing test",
        "Avoid loud noise for 24 hours before a hearing test, and let the",
        "audiologist know if your ears feel blocked on the day.",
        "",
        "Appointments and enquiries",
        "Appointments can be made by phone through hospital reception, or",
        "in person at the ENT outpatient desk. Please tell us the service",
        "you need by name so we can book the right slot length.",
    ],
]


def build_pdf(pages: list[list[str]]) -> bytes:
    """Assemble a text-based PDF with a correct cross-reference table."""
    objects: list[bytes] = []

    def add(body: bytes) -> None:
        objects.append(body)

    count = len(pages)
    page_nums = [4 + i * 2 for i in range(count)]
    content_nums = [5 + i * 2 for i in range(count)]

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, lines in enumerate(pages):
        add(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_nums[index]} 0 R "
                f"/Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        drawn = ["BT", "/F1 11 Tf", "15.5 TL", "60 730 Td"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            drawn.append(f"({escaped}) Tj T*")
        drawn.append("ET")
        stream = "\n".join(drawn).encode("latin-1")
        add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)


def main() -> int:
    data = build_pdf(PAGES)
    OUTPUT.write_bytes(data)

    # Read it back through the real pipeline, so a broken PDF is caught here
    # rather than at upload time.
    from clinic_front_desk.documents.text import chunk_text, extract_text

    extracted = extract_text(data, filename=str(OUTPUT))
    chunks = chunk_text(extracted)

    print(f"wrote {OUTPUT}  ({len(data):,} bytes, {extracted.page_count} pages)")
    print(f"reads back as {len(chunks)} retrievable passages:\n")
    for text, page in chunks:
        heading = text.split("\n")[0][:56]
        print(f"  page {page}  {len(text):4d} chars  {heading}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
