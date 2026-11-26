import re
import os
import shutil

import pytesseract
from PIL import Image

tesseract_cmd = os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
if tesseract_cmd:
  pytesseract.pytesseract.tesseract_cmd = tesseract_cmd


def process_receipt_image(image_path: str) -> dict:
  try:
    # Open image and convert to RGB (handles CMYK JPEGs, PNGs, etc. safely)
    with Image.open(image_path) as img:
      image = img.convert("RGB")
      text = pytesseract.image_to_string(image)
  except Exception as e:
    raise ValueError(f"Could not process image file: {e}")

  # Extract total amount with regex
  total_match = re.search(r"(?:TOTAL|Total|Amount)[\s:]*\$?(\d+\.\d{2})", text)
  total_amount = float(total_match.group(1)) if total_match else None

  # Extract date with regex
  date_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", text)
  invoice_date = date_match.group(1) if date_match else None

  return {
      "raw_text": text,
      "total_amount": total_amount,
      "invoice_date": invoice_date,
  }