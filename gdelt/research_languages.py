"""Annotate saved headlines for a display-only filter; no network or model requests."""

import argparse
from importlib.metadata import version
from pathlib import Path

from research_evidence import load_evidence, render, write


# Count detected headline languages and record how much of the packet was classifiable.
def headline_languages(packet):
    from lingua import LanguageDetectorBuilder

    detector = (
        LanguageDetectorBuilder.from_all_languages().with_minimum_relative_distance(0.20).build()
    )
    titles = {
        doc["title"]
        for case in packet["articles"]["cases"]
        for key in ("query", "fused", "cosine", "bm25")
        if (doc := case.get(key)) and isinstance(doc.get("title"), str)
    }
    labels = {}
    for title in sorted(titles):
        language = detector.detect_language_of(title)
        labels[title] = {
            "code": language.iso_code_639_1.name.lower() if language else "und",
            "name": language.name.replace("_", " ").title() if language else "Unknown",
        }
    return {
        "schema": "gdelt-headline-languages-v1",
        "run_id": packet["summary"]["run_id"],
        "input_sha256": packet["input_sha256"],
        "method": {
            "detector": "lingua-language-detector",
            "version": version("lingua-language-detector"),
            "minimum_relative_distance": 0.20,
            "scope": "saved headline text only",
            "language_set": "all languages supported by the pinned detector",
            "limitations": "Automatic labels can be wrong, especially for short, mixed or unsupported languages. Unknown means missing text or detector abstention. Headline language does not verify the linked article body. The abstention threshold is not a calibrated confidence score.",
        },
        "display_only": True,
        "by_title": labels,
    }


# Add the language counts to the saved study and regenerate its presentation.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = load_evidence(args.output)
    labels = headline_languages(packet)
    write(args.output / "headline_languages.json", labels)
    render(args.output)
    cases = packet["articles"]["cases"]
    english_pairs = sum(
        all(
            labels["by_title"].get((case.get(key) or {}).get("title"), {}).get("code") == "en"
            for key in ("query", "fused")
        )
        for case in cases
    )
    print(
        f"HEADLINE_LANGUAGES_READY: {english_pairs} of {len(cases)} saved current stories have both displayed headlines detected as English; full-cohort analytics unchanged.",
        flush=True,
    )


if __name__ == "__main__":
    main()
