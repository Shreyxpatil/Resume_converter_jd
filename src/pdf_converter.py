import os
import shutil
import subprocess
import sys
import tempfile

from pypdf import PdfReader


def count_pdf_pages(pdf_path: str) -> int:
    """Returns the real, rendered page count of a PDF — used to verify a
    generated resume actually fits within the app's 2-page limit, since a
    DOCX file has no page count of its own until something renders it."""
    return len(PdfReader(pdf_path).pages)


def _find_libreoffice_executable():
    """Finds LibreOffice executable if available on the system."""
    # 1. Check environment variables
    env_path = os.getenv("SOFFICE_PATH") or os.getenv("LIBREOFFICE_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Check system PATH
    which_soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if which_soffice:
        return which_soffice

    # 3. Check common Windows installation paths
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\LibreOffice\program\soffice.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path

    # 4. Check common macOS / Linux paths
    elif sys.platform == "darwin":
        mac_path = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if os.path.isfile(mac_path):
            return mac_path
    else:
        for linux_path in ["/usr/bin/soffice", "/usr/bin/libreoffice", "/usr/local/bin/soffice"]:
            if os.path.isfile(linux_path):
                return linux_path

    return None


def _convert_with_word_com(docx_path: str, target_pdf_path: str):
    """Converts DOCX to PDF using Microsoft Word COM automation (Windows only)."""
    if sys.platform != "win32":
        raise RuntimeError("Microsoft Word COM conversion is only supported on Windows.")

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise ImportError("pywin32 is not installed. Install with `pip install pywin32`.")

    abs_docx = os.path.abspath(docx_path)
    abs_pdf = os.path.abspath(target_pdf_path)
    os.makedirs(os.path.dirname(abs_pdf), exist_ok=True)

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        doc = word.Documents.Open(abs_docx, ReadOnly=True)
        # 17 = wdFormatPDF
        doc.SaveAs(abs_pdf, FileFormat=17)
        if not os.path.exists(abs_pdf):
            raise Exception("Microsoft Word did not produce the expected PDF file.")
        return abs_pdf
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _convert_with_libreoffice(docx_path: str, output_dir: str, soffice_cmd: str):
    """Converts DOCX to PDF using headless LibreOffice."""
    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        cmd = [
            soffice_cmd,
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            docx_path,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except Exception as e:
            raise Exception(f"LibreOffice execution failed: {e}")

        if result.returncode != 0:
            raise Exception(f"LibreOffice PDF conversion failed: {result.stderr.strip()[:300]}")

        base = os.path.splitext(os.path.basename(docx_path))[0]
        pdf_path = os.path.join(output_dir, base + ".pdf")
        if not os.path.exists(pdf_path):
            raise Exception("LibreOffice conversion did not produce the expected output file.")
        return pdf_path
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def convert_docx_to_pdf(docx_path: str, output_dir: str) -> str:
    """Converts a .docx file to .pdf using available engines (MS Word COM or LibreOffice).

    Returns the resulting PDF path.
    """
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"Input file not found: {docx_path}")

    base = os.path.splitext(os.path.basename(docx_path))[0]
    target_pdf_path = os.path.join(output_dir, base + ".pdf")
    return convert_docx_to_pdf_as(docx_path, target_pdf_path)


def convert_docx_to_pdf_as(docx_path: str, target_pdf_path: str) -> str:
    """Converts a .docx file to .pdf with a specific target path."""
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"Input file not found: {docx_path}")

    os.makedirs(os.path.dirname(os.path.abspath(target_pdf_path)), exist_ok=True)
    errors = []

    # Strategy 1: On Windows, try Microsoft Word COM first (if installed)
    if sys.platform == "win32":
        try:
            return _convert_with_word_com(docx_path, target_pdf_path)
        except Exception as e:
            errors.append(f"Microsoft Word COM: {e}")

    # Strategy 2: LibreOffice headless
    soffice_cmd = _find_libreoffice_executable()
    if soffice_cmd:
        out_dir = tempfile.mkdtemp(prefix="lo_pdfout_")
        try:
            produced = _convert_with_libreoffice(docx_path, out_dir, soffice_cmd)
            shutil.move(produced, target_pdf_path)
            return target_pdf_path
        except Exception as e:
            errors.append(f"LibreOffice: {e}")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
    else:
        errors.append("LibreOffice executable ('soffice') not found on system PATH or default directories.")

    # If all options failed
    err_detail = " | ".join(errors)
    raise Exception(
        f"PDF conversion failed across all available converters ({err_detail}).\n"
        "Please ensure either Microsoft Word (on Windows) or LibreOffice is installed."
    )
