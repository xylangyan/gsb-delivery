"""Vendor report preview selection over multipart/related structures."""

import base64
import time

import pytest

from mailparser import MailParser, build_preview, parse_from_bytes

PNG = b"\x89PNG\r\n\x1a\npng-payload"
SVG = b"<svg>stamp</svg>"
JPEG = b"\xff\xd8jpeg-payload"


def _image(cid, media_type="image/png", payload=None, disposition=None):
    from email.message import EmailMessage

    message = EmailMessage()
    maintype, subtype = media_type.split("/")
    message.set_content(
        PNG if payload is None else payload,
        maintype=maintype,
        subtype=subtype,
        cid=f"<{cid}>",
        disposition=disposition,
        cte="base64",
    )
    return message


def _html(srcs, cid=None):
    from email.message import EmailMessage

    tags = "".join(f'<img src="{src}">' for src in srcs)
    message = EmailMessage()
    message.set_content(f"<html><body>{tags}</body></html>", subtype="html")
    if cid:
        message.add_header("Content-ID", f"<{cid}>")
    return message


def _plain(text):
    from email.message import EmailMessage

    message = EmailMessage()
    message.set_content(text)
    return message


def _multipart(subtype, *children, params=None, disposition=None):
    from email.message import EmailMessage

    message = EmailMessage()
    message.set_type(f"multipart/{subtype}")
    for name, value in (params or {}).items():
        message.set_param(name, value)
    if disposition:
        message.add_header("Content-Disposition", disposition)
    for child in children:
        message.attach(child)
    return message


def _pack(*images):
    return _multipart("mixed", *images)


def _alternative(*branches):
    return _multipart("alternative", *branches)


def _section(*alternatives):
    return _multipart("mixed", *alternatives)


def _related(root, *children, start=None):
    params = {"start": f"<{start}>"} if start else None
    return _multipart("related", root, *children, params=params)


def _spec_message():
    """The flagship example: two HTML versions and two section packs."""
    old = _html(["cid:logo@v"])
    new = _html(["cid:logo@v", "cid:stamp@v"])
    root = _alternative(old, new)
    section_one = _section(
        _alternative(
            _pack(_image("logo@v", payload=PNG)),
            _pack(
                _image("logo@v", payload=PNG),
                _image("stamp@v", payload=PNG),
            ),
        )
    )
    section_two = _section(
        _alternative(
            _pack(_image("stamp@v", "image/svg+xml", SVG)),
            _pack(
                _image("logo@v", payload=PNG),
                _image("stamp@v", payload=PNG),
            ),
        )
    )
    return _related(root, section_one, section_two)


def test_prefers_latest_complete_html_and_earlier_alternatives():
    result = MailParser(_spec_message()).report_preview(("image/png", "image/svg+xml"))

    assert result["available"] is True
    assert result["body"] == {
        "media_type": "text/html",
        "part_path": (0, 1),
        "content_id": None,
    }
    # Logo comes from section 1's earlier branch; stamp from section 2's
    # earlier SVG branch, even though both sections have later packs.
    assert [(item["cid"], item["part_path"]) for item in result["images"]] == [
        ("logo@v", (1, 0, 0, 0)),
        ("stamp@v", (2, 0, 0, 0)),
    ]
    assert result["images"][0]["payload"] == PNG
    assert result["images"][1]["media_type"] == "image/svg+xml"
    assert result["images"][1]["payload"] == SVG


def test_disallowed_media_falls_back_to_older_html_and_later_branch():
    result = MailParser(_spec_message()).report_preview(("image/png",))

    assert result["available"] is True
    assert result["body"]["part_path"] == (0, 0)
    # The new HTML needs a stamp the SVG-only first branch cannot supply
    # under PNG-only policy, so the old HTML wins and logo uses the later
    # PNG pack of section 1.
    assert [(item["cid"], item["part_path"]) for item in result["images"]] == [
        ("logo@v", (1, 0, 1, 0)),
    ]
    assert result["unavailable"] == []


def test_forwarded_same_named_image_cannot_fill_reference():
    report = _spec_message()
    outer = _multipart("mixed", report, _image("stamp@v", payload=PNG))

    result = MailParser(outer).report_preview(("image/png",))

    assert result["available"] is True
    assert result["body"]["part_path"] == (0, 0, 0)
    assert [item["cid"] for item in result["images"]] == ["logo@v"]
    assert all(item["part_path"][0] == 0 for item in result["images"])


def test_direct_images_and_transfer_decoded_bytes():
    body = _html(["cid:logo@v"])
    report = _related(body, _image("logo@v", payload=PNG))

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["body"]["part_path"] == (0,)
    assert result["images"] == [
        {
            "cid": "logo@v",
            "part_path": (1,),
            "media_type": "image/png",
            "payload": PNG,
        }
    ]


def test_quoted_cid_url_is_unescaped_before_matching():
    body = _html(["cid:lo%67o%40v"])
    report = _related(body, _image("logo@v", payload=PNG))

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["images"][0]["cid"] == "logo@v"


def test_repeated_cid_reference_returns_one_image():
    body = _html(["cid:logo@v", "cid:logo@v"])
    report = _related(body, _image("logo@v", payload=PNG))

    result = MailParser(report).report_preview(("image/png",))

    assert len(result["images"]) == 1
    assert result["images"][0]["cid"] == "logo@v"


def test_duplicate_direct_image_breaks_exact_once_rule():
    body = _html(["cid:logo@v"])
    report = _related(
        body,
        _image("logo@v", payload=PNG),
        _image("logo@v", payload=PNG),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is False
    assert result["unavailable"][0]["reason"] == "unsatisfiable-cids"
    assert result["unavailable"][0]["cids"] == ["logo@v"]


def test_start_parameter_selects_named_root():
    first = _html(["cid:first@v"], cid="root-one")
    second = _html(["cid:second@v"], cid="root-two")
    report = _related(
        first,
        second,
        _image("first@v", payload=PNG),
        _image("second@v", payload=PNG),
        start="root-two",
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["body"]["part_path"] == (1,)
    assert [item["cid"] for item in result["images"]] == ["second@v"]


def test_unknown_start_is_structure_failure():
    report = _related(
        _html(["cid:first@v"]),
        _image("first@v", payload=PNG),
        start="missing@v",
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is False
    assert result["unavailable"] == [{"reason": "related-root-not-found"}]


def test_plain_text_fallback_keeps_html_diagnostics():
    report = _related(
        _alternative(
            _html(["cid:missing@v"]),
            _plain("plain report body"),
        ),
        _image("other@v", "image/jpeg", JPEG),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["body"]["media_type"] == "text/plain"
    assert result["body"]["part_path"] == (0, 1)
    assert result["images"] == []
    assert result["unavailable"] == [
        {"reason": "missing-image", "cids": ["missing@v"], "sections": []}
    ]


def test_no_usable_body_reports_missing_cids_with_sections():
    section = _section(_alternative(_pack(_image("stamp@v", "image/jpeg", JPEG))))
    report = _related(_html(["cid:stamp@v"]), section)

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is False
    assert result["body"] is None
    assert result["unavailable"] == [
        {
            "reason": "media-type-not-allowed",
            "cids": ["stamp@v"],
            "sections": [0],
            "media_types": ["image/jpeg"],
        }
    ]


def test_unsatisfiable_conflict_names_involved_sections():
    body = _html(["cid:x@v", "cid:y@v"])
    report = _related(
        body,
        _section(
            _alternative(
                _pack(_image("x@v"), _image("y@v")),
                _pack(_image("x@v")),
            )
        ),
        _section(_alternative(_pack(_image("x@v")))),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is False
    reason = result["unavailable"][0]
    assert reason["reason"] == "unsatisfiable-cids"
    assert reason["cids"] == ["y@v"]
    assert reason["sections"] == [0]


def test_unreferenced_images_are_not_returned():
    body = _html(["cid:used@v"])
    report = _related(
        body,
        _image("used@v", payload=PNG),
        _image("unused@v", payload=SVG),
    )

    result = MailParser(report).report_preview(("image/png", "image/svg+xml"))

    assert [item["cid"] for item in result["images"]] == ["used@v"]


def test_disallowed_direct_image_yields_to_section_copy():
    body = _html(["cid:logo@v"])
    report = _related(
        body,
        _image("logo@v", "image/jpeg", JPEG),
        _section(_alternative(_pack(_image("logo@v", "image/png", PNG)))),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["images"][0]["media_type"] == "image/png"
    assert result["images"][0]["part_path"] == (2, 0, 0, 0)


def test_disallowed_direct_image_without_section_copy_is_diagnosed():
    body = _html(["cid:logo@v"])
    report = _related(
        body,
        _image("logo@v", "image/jpeg", JPEG),
        _section(_alternative(_pack(_image("other@v", "image/png", PNG)))),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is False
    assert result["unavailable"][0]["reason"] == "media-type-not-allowed"
    assert result["unavailable"][0]["cids"] == ["logo@v"]
    assert result["unavailable"][0]["sections"] == []


def test_outer_mixed_requires_unique_non_attachment_related():
    report = _related(_html(["cid:logo@v"]), _image("logo@v", payload=PNG))
    outer = _multipart("mixed", report)

    result = MailParser(outer).report_preview(("image/png",))

    assert result["available"] is True
    assert result["body"]["part_path"] == (0, 0)
    assert result["images"][0]["part_path"] == (0, 1)

    two_related = _multipart("mixed", report, _multipart("related"))
    assert (
        build_preview(two_related, ("image/png",))["unavailable"][0]["reason"]
        == "report-structure-not-found"
    )

    attached = _multipart(
        "mixed",
        _multipart(
            "related",
            _html(["cid:logo@v"]),
            _image("logo@v", payload=PNG),
            disposition="attachment",
        ),
    )
    assert (
        MailParser(attached).report_preview(("image/png",))["unavailable"][0]["reason"]
        == "report-structure-not-found"
    )


def test_message_without_related_structure_fails_cleanly():
    plain = _plain("ordinary message")

    result = MailParser(plain).report_preview(("image/png",))

    assert result["available"] is False
    assert result["unavailable"] == [{"reason": "report-structure-not-found"}]


def test_later_alternative_branch_is_preferred_within_section():
    body = _html(["cid:a@v", "cid:b@v"])
    report = _related(
        body,
        _section(
            _alternative(
                _pack(_image("a@v", payload=b"first-a")),
                _pack(
                    _image("a@v", payload=b"second-a"),
                    _image("b@v", payload=b"second-b"),
                ),
            )
        ),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["images"][0]["payload"] == b"second-a"
    assert result["images"][1]["payload"] == b"second-b"


def test_branch_pack_with_duplicate_cid_is_rejected():
    body = _html(["cid:a@v"])
    report = _related(
        body,
        _section(
            _alternative(
                _pack(_image("a@v"), _image("a@v")),
                _pack(_image("a@v", payload=b"single")),
            )
        ),
    )

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["images"][0]["payload"] == b"single"


def test_html_without_cid_references_needs_no_images():
    report = _related(_html(["https://example.test/banner.png"]))

    result = MailParser(report).report_preview(("image/png",))

    assert result["available"] is True
    assert result["images"] == []


def test_existing_body_and_attachments_views_are_unchanged():
    report = _related(
        _alternative(
            _html(["cid:logo@v"]),
            _plain("plain fallback"),
        ),
        _image("logo@v", payload=PNG),
    )
    parser = MailParser(report)

    result = parser.report_preview(("image/png",))

    assert result["available"] is True
    assert parser.text_plain == ["plain fallback\n"]
    assert parser.text_html and "cid:logo@v" in parser.text_html[0]
    assert parser.attachments and parser.attachments[0]["content-id"] == ("<logo@v>")
    assert base64.b64decode(parser.attachments[0]["payload"]) == PNG


def test_string_media_type_argument_is_rejected():
    parser = MailParser(_related(_html(["cid:logo@v"])))

    with pytest.raises(TypeError):
        parser.report_preview("image/png")


def test_raw_bytes_message_uses_original_mime_positions():
    report = _related(
        _html(["cid:logo@v"]),
        _image("logo@v", payload=PNG),
    ).as_bytes()

    result = parse_from_bytes(report).report_preview(("image/png",))

    assert result["body"]["part_path"] == (0,)
    assert result["images"][0]["part_path"] == (1,)
    assert result["images"][0]["payload"] == PNG


def _chain_report(section_count, complete_back):
    """Build the pinned-cover/pinned-back monthly report shape."""
    refs = [f"cid:c{index}" for index in range(section_count + 1)]
    root = _html(refs)
    report = _multipart("related", root)
    for index in range(section_count):
        if index == 0:
            branches = [_pack(_image("c0"))] * 3
        elif index == section_count - 1:
            branches = (
                [
                    _pack(_image(f"c{section_count - 1}")),
                    _pack(_image(f"c{section_count}")),
                ]
                if complete_back
                else [_pack(_image(f"c{section_count - 1}"))]
            )
        else:
            branches = [
                _pack(_image(f"c{index}")),
                _pack(_image(f"c{index}"), _image(f"c{index + 1}")),
                _pack(_image(f"c{index + 1}")),
            ]
        report.attach(_section(_alternative(*branches)))
    return report


@pytest.mark.parametrize("complete_back", [False, True])
def test_long_monthly_report_selects_within_five_seconds(complete_back):
    report = _chain_report(120, complete_back)
    parser = MailParser(report)

    start = time.perf_counter()
    result = parser.report_preview(("image/png",))
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0
    if complete_back:
        assert result["available"] is True
        assert len(result["images"]) == 121
        paths = [item["part_path"] for item in result["images"]]
        assert len(set(paths)) == len(paths)
        assert [item["cid"] for item in result["images"]] == [
            f"c{index}" for index in range(121)
        ]
    else:
        assert result["available"] is False
        assert result["unavailable"][0]["cids"] == ["c120"]
