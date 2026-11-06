Aadhaar PDF Renamer Bot (duplicate filtering)

- Accepts multiple PDFs in a batch.
- Detects a 12-digit candidate in selectable PDF text.
- Skips exact duplicate numbers within the current batch.
- Keeps the first PDF for each unique number.
- Names output PDFs using only the last four digits: Aadhaar_XXXX1234.pdf.
- Sends the unique renamed PDFs in one ZIP.
- Temporary batch files are deleted after /done or /cancel.

Privacy: The complete number is compared in memory only and is not printed in messages or filenames. Use only for documents you are authorized to process. Avoid public/shared bots for identity documents.

Limitations: Scanned/image-only PDFs need OCR and are not supported by this version. The detector looks for a 12-digit candidate in selectable text; verify results before relying on them.

Render deployment:
