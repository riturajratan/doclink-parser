from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf
from docling.document_converter import DocumentConverter
from PIL import Image, ImageDraw, ImageFont

from app import append_image_ocr_sections, convert_pdf_to_markdown


def write_sample_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Docling smoke test")
    page.insert_text((72, 96), "Invoice Number: INV-42")
    page.insert_text((72, 120), "Total Amount: Rs 199.00")
    document.save(path)
    document.close()


def load_logo_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def write_logo_image_pdf(path: Path) -> None:
    image = Image.new("RGB", (1000, 260), "white")
    draw = ImageDraw.Draw(image)
    font = load_logo_font(72)
    draw.rounded_rectangle((24, 36, 190, 202), radius=28, fill="#f26b21")
    draw.text((230, 78), "ICICI BANK", fill="black", font=font)

    png_path = path.with_suffix(".png")
    image.save(png_path)

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(48, 48, 548, 178), filename=str(png_path))
    page.insert_text((48, 230), "Statement summary below.")
    document.save(path)
    document.close()


def write_mixed_text_and_logo_pdf(path: Path) -> None:
    image = Image.new("RGB", (1000, 260), "white")
    draw = ImageDraw.Draw(image)
    font = load_logo_font(72)
    draw.rounded_rectangle((24, 36, 190, 202), radius=28, fill="#f26b21")
    draw.text((230, 78), "ICICI BANK", fill="black", font=font)

    png_path = path.with_suffix(".png")
    image.save(png_path)

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(48, 48, 548, 178), filename=str(png_path))
    page.insert_text((48, 240), "15/03/2026| 19:05")
    page.insert_text((48, 264), "PYU*Swiggy FoodBangalore")
    page.insert_text((48, 288), "Rs 720.00")
    page.insert_text((48, 312), "l")
    document.save(path)
    document.close()


def main() -> int:
    converter = DocumentConverter()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        markdown_file = root / "sample.md"
        markdown_file.write_text(
            "# Smoke Test\n\nDocling should read this markdown file.\n",
            encoding="utf-8",
        )

        html_file = root / "sample.html"
        html_file.write_text(
            "<html><body><h1>HTML Smoke Test</h1><p>Docling should read this HTML file.</p></body></html>",
            encoding="utf-8",
        )

        pdf_file = root / "sample.pdf"
        write_sample_pdf(pdf_file)

        logo_pdf_file = root / "logo-only.pdf"
        write_logo_image_pdf(logo_pdf_file)

        mixed_pdf_file = root / "mixed-text-and-logo.pdf"
        write_mixed_text_and_logo_pdf(mixed_pdf_file)

        samples = {
            markdown_file: ["Smoke Test", "Docling should read this markdown file."],
            html_file: ["HTML Smoke Test", "Docling should read this HTML file."],
            pdf_file: ["Docling smoke test", "Invoice Number: INV-42", "Total Amount: Rs 199.00"],
        }

        failures: list[str] = []
        for sample_path, expected_fragments in samples.items():
            markdown = converter.convert(sample_path).document.export_to_markdown()
            missing = [fragment for fragment in expected_fragments if fragment not in markdown]
            if missing:
                failures.append(f"{sample_path.name}: missing {missing}")
                continue
            print(f"[ok] {sample_path.name}")

        logo_markdown, _ = convert_pdf_to_markdown(
            logo_pdf_file,
            document_title="Logo OCR Test",
            ocr_mode="selective",
        )
        if "ICICI BANK" not in logo_markdown:
            failures.append("logo-only.pdf: selective OCR did not extract `ICICI BANK` from the embedded image")
        else:
            print("[ok] logo-only.pdf image OCR")

        mixed_without_ocr, _ = convert_pdf_to_markdown(
            mixed_pdf_file,
            document_title="Mixed OCR Test",
            ocr_mode="off",
        )
        mixed_with_ocr, _ = convert_pdf_to_markdown(
            mixed_pdf_file,
            document_title="Mixed OCR Test",
            ocr_mode="selective",
        )
        merged_page_markdown = append_image_ocr_sections(
            "# Mixed OCR Test\n\n## Page 1\n\nTransaction table here.",
            mixed_pdf_file,
        )
        if "## Page 1" not in mixed_with_ocr:
            failures.append("mixed-text-and-logo.pdf: selective OCR did not produce page-wise markdown")
        elif "PYU*Swiggy FoodBangalore" not in mixed_with_ocr or "Rs 720.00" not in mixed_with_ocr:
            failures.append("mixed-text-and-logo.pdf: selective OCR lost the base text content")
        elif "ICICI BANK" not in mixed_with_ocr:
            failures.append("mixed-text-and-logo.pdf: selective OCR did not merge image text into the markdown")
        elif "## Page 1\n\nTransaction table here.\n\n### Image OCR" not in merged_page_markdown:
            failures.append("mixed-text-and-logo.pdf: page-level markdown did not keep image OCR inline with the matching page")
        else:
            print("[ok] mixed-text-and-logo.pdf keeps base markdown and adds image OCR")

        if failures:
            print("[failed] Docling smoke test did not match expected text:")
            for failure in failures:
                print(f" - {failure}")
            return 1

    print("[ok] Docling can read the local smoke-test documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
