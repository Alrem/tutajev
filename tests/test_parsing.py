from __future__ import annotations

from tests.conftest import load_eml
from tutajev.models import Address, AttachmentMeta, RawMessage
from tutajev.parsing import extract_urls, html_to_text, parse_message


def test_plain_message() -> None:
    summary = parse_message(load_eml("plain.eml"))

    assert summary.sender == Address("Alice Example", "alice@example.com")
    assert summary.reply_to == ()
    assert summary.reply_to_differs is False
    assert summary.subject == "Plain hello"
    assert summary.text_body.startswith("Hi Bob,")
    assert summary.urls == ("https://example.com/page?a=1", "www.example.net/x")
    assert summary.attachments == ()
    assert summary.has_html is False
    assert summary.date is not None and summary.date.year == 2026


def test_html_only_message_decodes_headers_and_extracts_links() -> None:
    summary = parse_message(load_eml("html_only.eml"))

    assert summary.sender == Address("Призов", "prize@lottery.example")
    assert summary.reply_to == (Address("Claims Desk", "claims@other.example"),)
    assert summary.reply_to_differs is True
    assert summary.subject == "You won €1000!"
    assert summary.has_html is True
    assert "Claim your prize now:" in summary.text_body
    assert "color:red" not in summary.text_body
    assert "evil.example" not in summary.text_body
    assert summary.urls == ("https://claim.example/win?id=7", "https://tracker.example/pixel.gif")


def test_multipart_with_attachments() -> None:
    summary = parse_message(load_eml("multipart_attachment.eml"))

    assert summary.text_body == "Invoice attached. Pay at https://shop.example/pay"
    assert summary.urls == ("https://shop.example/pay",)
    assert summary.reply_to_differs is False
    assert summary.has_html is True
    pdf, logo = summary.attachments
    assert pdf.filename == "invoice.pdf"
    assert pdf.content_type == "application/pdf"
    assert pdf.disposition == "attachment"
    assert pdf.size_bytes > 0
    assert logo == AttachmentMeta("logo.png", "image/png", 8, "inline", "logo@shop")


def test_backend_supplied_attachment_metadata_wins() -> None:
    meta = (AttachmentMeta("a.zip", "application/zip", 10),)
    raw = load_eml("plain.eml")
    summary = parse_message(RawMessage(raw.id, raw.received_at, raw.raw, attachments=meta))
    assert summary.attachments == meta


def test_extract_urls_strips_trailing_punctuation() -> None:
    assert extract_urls("Go to https://a.example/x). Or http://b.example!") == [
        "https://a.example/x",
        "http://b.example",
    ]


def test_html_to_text_ignores_non_http_links() -> None:
    text, urls = html_to_text('<a href="javascript:void(0)">x</a><a href="#top">y</a>')
    assert text == "xy"
    assert urls == []


def test_message_without_from() -> None:
    summary = parse_message(RawMessage("x", None, b"Subject: hi\r\n\r\nbody\r\n"))
    assert summary.sender is None
    assert summary.text_body == "body"
