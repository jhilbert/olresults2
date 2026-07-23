# Verified scan transcripts

These JSON files transcribe result PDFs that contain only page images and
therefore have no usable text layer for the normal parsers.

Each transcript is tied to the exact source attachment through
`sourceSha256`. The PDF parser refuses a transcript when ANNE's attachment
bytes have changed. This makes a historical correction explicit and
reproducible without making production builds depend on a particular OCR
engine.

The stored rows were produced from rendered PDF pages, reconciled against
each category's printed starter count, and visually reviewed. OCR output is
only a draft; the JSON in this directory is the reviewed input used by the
build.
