import pickle

import html2text
from html2text import StreamingHTML2Text


def _converter(**options):
    converter = StreamingHTML2Text()
    for key, value in options.items():
        setattr(converter, key, value)
    return converter


def test_streaming_matches_handle_for_arbitrary_chunks():
    html = (
        "<p>First paragraph with <a href='/u' title='T'>same</a></p>"
        "<p>Second paragraph with enough text " + "word " * 30 + "</p>"
        "<p>Third <a href='/u' title='T'>same</a> "
        "<a href='/u' title='Other'>other</a></p>"
    )
    options = {"inline_links": False}
    expected_converter = html2text.HTML2Text()
    expected_converter.inline_links = False
    expected = expected_converter.handle(html)

    for size in range(1, 12):
        converter = _converter(**options)
        result = []
        for start in range(0, len(html), size):
            converter.feed(html[start : start + size])
            result.append(converter.read())
        result.append(converter.finish())
        assert "".join(result) == expected


def test_third_paragraph_is_available_before_reference_definitions():
    converter = StreamingHTML2Text(inline_links=False)
    batches = []
    for paragraph in [
        '<p>One <a href="https://x" title="T">x</a></p>',
        "<p>Two</p>",
        '<p>Three <a href="https://x" title="T">x</a></p>',
    ]:
        converter.feed(paragraph)
        batches.append(converter.read())

    body = "".join(batches)
    assert "One [x][1]" in body
    assert "Three [x][1]" in body
    assert "   [1]:" not in body

    definitions = converter.finish()
    assert "[1]: https://x (T)" in definitions


def test_duplicate_link_identity_follows_title_rules():
    html = (
        '<a href="/u" title="Same">a</a>'
        '<a href="/u" title="Same">b</a>'
        '<a href="/u" title="Different">c</a>'
    )
    converter = StreamingHTML2Text(inline_links=False, body_width=0)
    output = converter.handle(html)
    assert "[a][1]" in output
    assert "[b][1]" in output
    assert "[c][2]" in output


def test_table_rows_are_withheld_until_table_closes():
    html = (
        "<p>Before</p>"
        "<table><tr><th>Short</th></tr>"
        + "".join(f"<tr><td>row{i}</td></tr>" for i in range(10))
        + "</table>"
    )
    cut = html.find("<table>") + len("<table>") + 120
    converter = StreamingHTML2Text(pad_tables=True)
    converter.feed(html[:cut])
    early = converter.read()
    assert "Before" in early
    assert "row0" not in early
    assert "|" not in early

    output = early + converter.send(html[cut:])
    output += converter.finish()
    expected = html2text.HTML2Text()
    expected.pad_tables = True
    assert output == expected.handle(html)


def test_spooled_table_matches_padded_output(tmp_path):
    html = (
        "<table><tr><th>A</th><th>B</th></tr>"
        + "".join(
            f"<tr><td>{'x' * (i % 17)}</td><td>row{i}</td></tr>"
            for i in range(80)
        )
        + "</table>"
    )
    expected_converter = html2text.HTML2Text()
    expected_converter.pad_tables = True
    expected = expected_converter.handle(html)

    converter = StreamingHTML2Text(
        pad_tables=True,
        memory_limit=16,
        spool_dir=str(tmp_path),
    )
    output = converter.handle(html)
    assert output == expected
    assert list(tmp_path.glob("html2text-table-*.txt"))


def test_checkpoint_resumes_half_table_and_links(tmp_path):
    html = (
        '<p><a href="/u" title="T">x</a></p>'
        "<p>Before table</p>"
        "<table><tr><th>A</th><th>B</th></tr>"
        + "".join(f"<tr><td>{'x' * i}</td><td>{i}</td></tr>" for i in range(20))
        + "</table>"
        '<p><a href="/u" title="T">x</a></p>'
    )
    cut = html.find("<tr><td>xx")
    expected_converter = html2text.HTML2Text()
    expected_converter.pad_tables = True
    expected_converter.inline_links = False
    expected = expected_converter.handle(html)

    first = StreamingHTML2Text(
        pad_tables=True,
        inline_links=False,
        memory_limit=64,
        spool_dir=str(tmp_path),
    )
    prefix_output = first.send(html[:cut])
    checkpoint = first.checkpoint().dumps()
    assert first.source_position == cut

    resumed = StreamingHTML2Text.from_checkpoint(checkpoint)
    assert resumed.source_position == cut
    output = prefix_output + resumed.read()
    output += resumed.send(html[cut:])
    output += resumed.finish()

    assert output == expected
    assert "[x][1]" in output
    assert output.count("[1]") >= 3


def test_pickle_session_and_byte_split_utf8():
    html = "<p>中文–😀 " + "word " * 20 + "</p>"
    raw = html.encode("utf-8")
    expected = html2text.html2text(html)

    converter = StreamingHTML2Text()
    output = ""
    cut = len(raw) // 2
    converter.feed(raw[:cut])
    output += converter.read()
    restored = pickle.loads(pickle.dumps(converter))
    restored._output_callback = None

    position = cut
    for end in range(cut + 3, len(raw), 3):
        restored.feed(raw[position:end])
        position = end
        output += restored.read()
    restored.feed(raw[position:])
    output += restored.read() + restored.finish()

    assert output == expected
    assert restored.source_position == len(raw)


def test_old_one_shot_handle_still_works():
    assert html2text.html2text("<p>Hello</p>") == "Hello\n\n"
