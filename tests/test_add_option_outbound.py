import json
import pickle

import pytest

from loguru import logger


class JsonSink:
    def __init__(self):
        self.records = []
        self.json = []

    def write(self, message):
        self.records.append(message.record)
        self.json.append(json.loads(message))


def collect(sink_list):
    def sink(message):
        sink_list.append(message)

    return sink


def make_internal_formatter(seen):
    def formatter(record):
        record["extra"]["internal_summary"] = "internal:" + record["extra"]["summary"]["ticket"]
        seen.append(dict(record["extra"]))
        return "{message}\n"

    return formatter


def test_fields_mask_and_shared_value():
    internal = []
    external = []

    logger.add(collect(internal), format="{message}", colorize=False)
    logger.add(
        collect(external),
        format="{message}",
        colorize=False,
        outbound={"fields": ["ticket", "station"], "mask": ["ticket"]},
    )

    logger.bind(ticket="PX7", station="PX7").info(
        "{ticket} at {station}", ticket="PX7", station="PX7"
    )

    assert [str(message) for message in internal] == ["PX7 at PX7\n"]
    assert [str(message) for message in external] == ["*** at PX7\n"]
    assert external[0].record["extra"]["station"] == "PX7"
    assert external[0].record["extra"]["ticket"] == "***"


def test_projection_of_shared_dict_per_sink():
    internal = []
    seen = []
    external = []
    serialized = JsonSink()

    logger.add(collect(internal), format=make_internal_formatter(seen), colorize=False)
    logger.add(
        collect(external),
        format=lambda record: "{message}",
        colorize=False,
        outbound={
            "fields": ["ticket", "summary.ticket", "summary.amount"],
            "mask": ["ticket", "summary.ticket"],
        },
    )
    logger.add(
        serialized,
        format="{message}",
        serialize=True,
        outbound={
            "fields": ["ticket", "summary.ticket", "summary.amount"],
            "mask": ["ticket", "summary.ticket"],
        },
    )

    shared = {"ticket": "T-1", "amount": 5, "station": "S-9"}
    logger.info(
        "ticket={ticket} nested={summary[ticket]} amount={summary[amount]}",
        summary=shared,
        ticket="PX7",
    )

    external_record = external[0].record
    assert external_record["extra"] == {
        "ticket": "***",
        "summary": {"ticket": "***", "amount": 5},
    }
    assert external_record["message"] == "ticket=*** nested=*** amount=5"
    assert serialized.json[0]["record"]["extra"] == external_record["extra"]
    assert serialized.json[0]["text"] == "ticket=*** nested=*** amount=5\n"
    assert internal[0].record["extra"] == {
        "ticket": "PX7",
        "summary": {"ticket": "T-1", "amount": 5, "station": "S-9"},
        "internal_summary": "internal:T-1",
    }


def test_external_does_not_see_internal_summary():
    internal = []
    seen = []
    external = []

    logger.add(collect(internal), format=make_internal_formatter(seen), colorize=False)
    logger.add(
        collect(external),
        format=lambda record: "{message}",
        colorize=False,
        outbound={"fields": ["ticket"]},
    )

    logger.bind(ticket="T-1", summary={"ticket": "T-1"}).info("go")
    logger.bind(ticket="T-1", summary={"ticket": "T-1"}).info("go")

    assert "internal_summary" not in external[1].record["extra"]
    assert "internal_summary" not in external[0].record["extra"]
    assert seen == [
        {"ticket": "T-1", "summary": {"ticket": "T-1"}, "internal_summary": "internal:T-1"},
        {"ticket": "T-1", "summary": {"ticket": "T-1"}, "internal_summary": "internal:T-1"},
    ]


def test_lazy_argument_evaluated_once_for_both_sides():
    internal = []
    external = []
    generated = []

    def ticket():
        generated.append(len(generated) + 1)
        return "PX%d" % generated[-1]

    logger.add(collect(internal), format="{message}", colorize=False)
    logger.add(
        collect(external),
        format="{message}",
        colorize=False,
        outbound={"fields": ["ticket"], "mask": ["ticket"]},
    )

    logger.opt(lazy=True).info("ticket={ticket}", ticket=ticket)

    assert generated == [1]
    assert str(internal[0]) == "ticket=PX1\n"
    assert str(external[0]) == "ticket=***\n"


def test_queue_uses_snapshot_taken_before_send():
    internal = []
    external = []

    def internal_sink(message):
        internal.append(dict(message.record["extra"]))

    def external_sink(message):
        external.append(dict(message.record["extra"]))

    logger.add(internal_sink, format="{message}", colorize=False, enqueue=True)
    logger.add(
        external_sink,
        format="{message}",
        colorize=False,
        enqueue=True,
        outbound={"fields": ["ticket"]},
    )

    logger.bind(ticket="PX7", summary={"ticket": "T-1"}).info("go")
    logger.complete()
    logger.remove()

    assert internal == [{"ticket": "PX7", "summary": {"ticket": "T-1"}}]
    assert external == [{"ticket": "PX7"}]


def test_outbound_error_record_is_projected(capsys):
    external = []

    def failing_formatter(record):
        return "{forbidden}\n"

    logger.add(lambda m: None, format="{message}", colorize=False)
    logger.add(
        collect(external),
        format=failing_formatter,
        colorize=False,
        catch=True,
        outbound={"fields": ["ticket"], "mask": ["ticket"]},
    )

    logger.info("ticket={ticket}", ticket="PX7")

    err = capsys.readouterr().err
    assert "--- Logging error in Loguru Handler #1 ---" in err
    assert "'extra': {'ticket': '***'}" in err
    assert "summary" not in err
    assert external == []
    assert "PX7" not in err


def test_outbound_format_error_when_template_uses_excluded_field(capsys):
    external = []

    logger.add(lambda m: None, format="{message}", colorize=False)
    logger.add(
        collect(external),
        format="{message}",
        colorize=False,
        catch=True,
        outbound={"fields": ["ticket"], "mask": ["ticket"]},
    )

    logger.info("{ticket} at {station}", ticket="PX7", station="PX7")

    err = capsys.readouterr().err
    assert "--- Logging error in Loguru Handler #1 ---" in err
    assert "The outbound log message could not be formatted" in err
    assert "'extra': {'ticket': '***'}" in err
    assert external == []


def test_mask_applies_to_nested_dictionary_and_record_path():
    external = []

    logger.add(
        collect(external),
        format="{message}",
        colorize=False,
        outbound={"mask": {"summary.secret": "###", "[token]": "###"}},
    )

    logger.info(
        "{summary[secret]} {token}",
        summary={"secret": "ABC"},
        token="XYZ",
    )

    record = external[0].record
    assert record["extra"]["summary"]["secret"] == "###"
    assert record["extra"]["token"] == "###"
    assert str(external[0]) == "### ###\n"


def test_record_option_sees_projected_record():
    external = []

    logger.add(
        collect(external),
        format="{message}",
        colorize=False,
        outbound={"fields": ["ticket"], "mask": ["ticket"]},
    )

    logger.opt(record=True).info(
        "ticket={record[extra][ticket]} {record[level].name}", ticket="PX7"
    )

    record = external[0].record
    assert record["extra"] == {"ticket": "***"}
    assert str(external[0]) == "ticket=*** INFO\n"


def test_default_usage_is_unchanged():
    writer = []

    logger.add(collect(writer), format="{message}", colorize=False)
    logger.bind(value="PX7").info("value={value}", value="PX7")

    assert [str(message) for message in writer] == ["value=PX7\n"]


def test_invalid_outbound_configuration():
    with pytest.raises(TypeError, match=r"Invalid outbound configuration"):
        logger.add(lambda m: None, outbound=123)


def _picklable_sink(message):
    pass


def test_outbound_is_picklable():
    logger.add(
        _picklable_sink,
        format="{message}",
        colorize=False,
        outbound={"fields": ["ticket"], "mask": ["ticket"]},
    )

    dupe = pickle.loads(pickle.dumps(logger))
    assert repr(dupe).startswith("<loguru.logger handlers=[(id=0")
    dupe.info("{ticket}", ticket="PX7")
    logger.remove()
