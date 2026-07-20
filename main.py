"""
Cloud Function (HTTP, gen2): polls a Google Drive "inbox" folder and, for
every PDF or EPUB it finds:
  - PDF        -> converted to a reflowable EPUB, then to KEPUB
  - EPUB       -> converted straight to KEPUB
  - .kepub.epub -> already a KEPUB, just moved as-is

Runs entirely as the attached service account via IAM (Application
Default Credentials) — no key file exists anywhere in this design.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
import functions_framework
import google.auth
from ebooklib import epub
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

INBOX_FOLDER_ID = os.environ["INBOX_FOLDER_ID"]
CONVERTED_FOLDER_ID = os.environ["CONVERTED_FOLDER_ID"]
FAILED_FOLDER_ID = os.environ["FAILED_FOLDER_ID"]

KEPUBIFY = str(Path(__file__).resolve().parent / "kepubify")

STYLE = """
body { font-family: serif; line-height: 1.4; margin: 1em; }
h1 { font-size: 1.3em; }
p { margin: 0 0 0.8em 0; text-align: justify; }
"""


def drive_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_files(service, folder_id):
    resp = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name)",
            pageSize=100,
        )
        .execute()
    )
    return resp.get("files", [])


def download(service, file_id, dest: Path) -> None:
    request = service.files().get_media(fileId=file_id)
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def replace_in_place(service, file_id: str, new_name: str, new_content: Path, from_folder: str, to_folder: str) -> None:
    """Overwrites an existing file's content and name, and relocates it,
    all in one call, without ever creating a new file object. This is the
    part that avoids storageQuotaExceeded: the file keeps its original
    owner (the human who uploaded it) throughout, since only create()
    assigns a new owner/quota, update() never does."""
    media = MediaFileUpload(str(new_content), resumable=False)
    service.files().update(
        fileId=file_id,
        body={"name": new_name},
        media_body=media,
        addParents=to_folder,
        removeParents=from_folder,
        fields="id",
    ).execute()


def move(service, file_id: str, from_folder: str, to_folder: str) -> None:
    """Relocates a file without touching its content, used for the
    already-a-kepub pass-through and the failure path."""
    service.files().update(
        fileId=file_id, addParents=to_folder, removeParents=from_folder, fields="id"
    ).execute()


def pdf_to_epub(pdf_path: Path, out_path: Path) -> None:
    doc = fitz.open(pdf_path)
    book = epub.EpubBook()
    title = pdf_path.stem
    book.set_identifier(title)
    book.set_title(title)
    book.set_language("en")

    css = epub.EpubItem(
        uid="style", file_name="style.css", media_type="text/css", content=STYLE
    )
    book.add_item(css)

    chapters = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if not text:
            continue
        paragraphs = "".join(
            f"<p>{p.strip()}</p>" for p in re.split(r"\n{2,}", text) if p.strip()
        )
        c = epub.EpubHtml(title=f"Page {i + 1}", file_name=f"page_{i + 1}.xhtml", lang="en")
        c.content = f"<h1>{title} — page {i + 1}</h1>{paragraphs}"
        c.add_item(css)
        book.add_item(c)
        chapters.append(c)

    if not chapters:
        raise ValueError("no extractable text in PDF (likely a scanned/image PDF)")

    book.toc = tuple(chapters)
    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(out_path), book)


def epub_to_kepub(epub_path: Path, out_path: Path) -> None:
    os.chmod(KEPUBIFY, 0o755)  # deployment can drop the executable bit; cheap to re-set
    result = subprocess.run(
        [KEPUBIFY, str(epub_path), "-o", str(out_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kepubify failed: {result.stderr.strip()}")


def run(service) -> list[str]:
    """Does one poll-and-convert pass. Returns a log of what happened."""
    log: list[str] = []
    files = list_files(service, INBOX_FOLDER_ID)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for f in files:
            name, fid = f["name"], f["id"]
            lower = name.lower()
            local_in = tmp / name

            try:
                download(service, fid, local_in)

                if lower.endswith(".kepub.epub"):
                    move(service, fid, INBOX_FOLDER_ID, CONVERTED_FOLDER_ID)

                elif lower.endswith(".epub"):
                    kepub_path = tmp / f"{local_in.name[:-len('.epub')]}.kepub.epub"
                    epub_to_kepub(local_in, kepub_path)
                    replace_in_place(service, fid, kepub_path.name, kepub_path, INBOX_FOLDER_ID, CONVERTED_FOLDER_ID)

                elif lower.endswith(".pdf"):
                    stem = local_in.stem
                    epub_path = tmp / f"{stem}.epub"
                    kepub_path = tmp / f"{stem}.kepub.epub"
                    pdf_to_epub(local_in, epub_path)
                    epub_to_kepub(epub_path, kepub_path)
                    replace_in_place(service, fid, kepub_path.name, kepub_path, INBOX_FOLDER_ID, CONVERTED_FOLDER_ID)

                else:
                    log.append(f"SKIPPED (not a .pdf/.epub): {name}")
                    continue

                log.append(f"OK: {name}")

            except Exception as e:
                log.append(f"FAILED: {name}: {e}")
                try:
                    move(service, fid, INBOX_FOLDER_ID, FAILED_FOLDER_ID)
                except Exception as move_err:
                    log.append(f"  (also couldn't move {name} to failed/: {move_err})")

    return log


@functions_framework.http
def process_inbox(request):
    service = drive_service()
    log = run(service)
    print("\n".join(log) if log else "Nothing new in inbox.")
    return {"log": log}, 200
