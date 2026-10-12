"""Effective limits by input entry point; these are not load-test claims."""

FORMAL_UPLOAD_FILE_BYTES = 5 * 1024 * 1024
FORMAL_UPLOAD_DOCUMENTS = 20
FORMAL_PASTED_TEXT_BYTES = 200_000
CLOUD_INPUT_FILE_BYTES = 500 * 1024 * 1024
CLOUD_RUN_INPUT_BYTES = 2 * 1024 * 1024 * 1024


def formal_upload_limits(
    *, max_file_bytes: int = FORMAL_UPLOAD_FILE_BYTES,
    max_documents: int = FORMAL_UPLOAD_DOCUMENTS,
) -> dict:
    return {
        "max_file_bytes": max_file_bytes,
        "max_documents": max_documents,
        "max_text_bytes": FORMAL_PASTED_TEXT_BYTES,
        "text_counts_as_document": True,
    }


def cloud_run_input_limits() -> dict:
    return {"max_file_bytes": CLOUD_INPUT_FILE_BYTES, "max_total_bytes": CLOUD_RUN_INPUT_BYTES}
