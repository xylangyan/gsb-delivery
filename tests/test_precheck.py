import itertools

from formencode import ForEach, Schema, validators
from formencode.precheck import precheck, PreCheckResult
from formencode.variabledecode import variable_decode, NestedVariables


class Item(Schema):
    qty = validators.Int(if_empty=0, if_missing=0)


class Rows(Schema):
    items = ForEach(Item)


def freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(val))
                            for key, val in value.items()))
    if isinstance(value, list):
        return tuple(freeze(val) for val in value)
    return value


def brute_force_count(schema, fragments):
    """Reference: enumerate every combination of readings, decode and
    validate each, and count the distinct resulting forms."""
    results = set()
    for combo in itertools.product(*[range(len(f)) for f in fragments]):
        flat = {}
        for frag_no, reading_no in enumerate(combo):
            reading = fragments[frag_no][reading_no]
            pairs = (reading.items() if hasattr(reading, 'items')
                     else reading)
            for name, value in pairs:
                if name in flat:
                    existing = flat[name]
                    if isinstance(existing, list):
                        existing.append(value)
                    else:
                        flat[name] = [existing, value]
                else:
                    flat[name] = value
        try:
            result = schema.to_python(variable_decode(flat))
        except Exception:
            continue
        results.add(freeze(result))
    return len(results)


def build_form(schema, fragments, assignment):
    flat = {}
    for frag_no, reading_no in enumerate(assignment):
        reading = fragments[frag_no][reading_no]
        pairs = (reading.items() if hasattr(reading, 'items')
                 else reading)
        for name, value in pairs:
            if name in flat:
                existing = flat[name]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    flat[name] = [existing, value]
            else:
                flat[name] = value
    return schema.to_python(variable_decode(flat))


class TestUniqueForm:

    def test_single_reading_per_fragment(self):
        class S(Schema):
            name = validators.String()
            qty = validators.Int()
        result = precheck(S(), [
            [{'name': 'apple'}],
            [{'qty': '3'}],
        ])
        assert result.count == 1
        assert result.unique
        assert result.form == {'name': 'apple', 'qty': 3}
        assert result.field is None
        assert result.choices is None

    def test_readings_collapse_to_same_form(self):
        class S(Schema):
            qty = validators.Int()
        # '3' and '03' both convert to 3: only one distinct form
        result = precheck(S(), [[{'qty': '3'}, {'qty': '03'}]])
        assert result.count == 1
        assert result.form == {'qty': 3}

    def test_blank_and_missing_share_default(self):
        class S(Schema):
            qty = validators.Int(if_empty=0, if_missing=0)
        # an unfilled control and a control scanned blank both
        # default to zero, so the two readings give the same form
        result = precheck(S(), [[{'qty': ''}, {}]])
        assert result.count == 1
        assert result.form == {'qty': 0}

    def test_form_matches_direct_decode(self):
        class S(Schema):
            a = validators.Int()
            b = validators.String(strip=True)
        fragments = [[{'a': '04'}], [{'b': ' hi '}, {'b': 'hi'}]]
        result = precheck(S(), fragments)
        assert result.count == 1
        assert result.form == {'a': 4, 'b': 'hi'}

    def test_no_fragments(self):
        class S(Schema):
            qty = validators.Int(if_missing=0)
        result = precheck(S(), [])
        assert result.count == 1
        assert result.form == {'qty': 0}


class TestNoValidForm:

    def test_required_field_missing(self):
        class S(Schema):
            req = validators.Int(not_empty=True)
        assert precheck(S(), [[{}]]).count == 0

    def test_required_field_unfillable(self):
        class S(Schema):
            req = validators.Int(not_empty=True)
            other = validators.String(if_missing='')
        # every reading only ever touches 'other'
        assert precheck(S(), [[{'other': 'x'}, {}]]).count == 0

    def test_all_readings_invalid(self):
        class S(Schema):
            qty = validators.Int(not_empty=True)
        result = precheck(S(), [[{'qty': 'abc'}, {'qty': ''}]])
        assert result.count == 0

    def test_extra_field_rejected(self):
        class S(Schema):
            a = validators.Int()
        assert precheck(S(), [[{'nope': '1'}]]).count == 0

    def test_extra_field_allowed(self):
        class S(Schema):
            allow_extra_fields = True
            a = validators.Int(if_missing=1)
        result = precheck(S(), [[{'nope': '1'}]])
        assert result.count == 1
        assert result.form == {'a': 1, 'nope': '1'}


class TestMultipleForms:

    def test_count_and_divergent_field(self):
        class S(Schema):
            a = validators.Int()
            b = validators.Int()
        fragments = [
            [{'a': '1'}, {'a': '2'}],
            [{'b': '1'}, {'b': '2'}],
        ]
        result = precheck(S(), fragments)
        assert result.count == 4
        assert not result.unique
        assert result.form is None
        assert result.field in ('a', 'b')
        first, second = result.choices
        assert len(first) == len(second) == 2
        form_a = build_form(S(), fragments, first)
        form_b = build_form(S(), fragments, second)
        assert form_a != form_b
        assert form_a[result.field] != form_b[result.field]
        assert (form_a[result.field], form_b[result.field]) == result.values

    def test_choices_are_valid_reading_indices(self):
        class S(Schema):
            x = validators.OneOf(['p', 'q'])
        fragments = [[{'x': 'p'}, {'x': 'q'}, {'x': 'r'}]]
        result = precheck(S(), fragments)
        assert result.count == 2
        for choice in result.choices:
            assert len(choice) == 1
            assert choice[0] in (0, 1)  # never the invalid reading
            build_form(S(), fragments, choice)


class TestFieldsMatch:

    def make_schema(self):
        class S(Schema):
            a = validators.Int()
            b = validators.Int()
            chained_validators = [validators.FieldsMatch('a', 'b')]
        return S()

    def test_match_rule_couples_fields(self):
        schema = self.make_schema()
        fragments = [
            [{'a': '1'}, {'a': '2'}],
            [{'b': '1'}, {'b': '2'}],
        ]
        # each field alone always validates; only the matching
        # combinations survive on one form
        assert precheck(schema, fragments).count == 2
        assert brute_force_count(schema, fragments) == 2

    def test_mismatch_only(self):
        schema = self.make_schema()
        fragments = [[{'a': '1'}], [{'b': '2'}]]
        assert precheck(schema, fragments).count == 0

    def test_raw_strings_compared_before_conversion(self):
        # FieldsMatch.validate_partial compares the raw values, so
        # '1' and '01' do not match even though both convert to 1
        schema = self.make_schema()
        fragments = [[{'a': '1'}], [{'b': '01'}]]
        assert precheck(schema, fragments).count == 0


class TestRepeatedRows:

    def test_sparse_rows_shift_forward(self):
        # the scanner is unsure whether the row is the second or the
        # third; with nothing else on the form both readings decode to
        # the same single-row list
        result = precheck(Rows(), [
            [{'items-2.qty': '5'}, {'items-3.qty': '5'}],
        ])
        assert result.count == 1
        assert result.form == {'items': [{'qty': 5}]}

    def test_missing_row_changes_positions(self):
        fragments = [
            [{'items-1.qty': '1'}, {}],
            [{'items-3.qty': '2'}],
        ]
        # row 3 shifts into position 2 when row 1 is absent
        result = precheck(Rows(), fragments)
        assert result.count == 2
        assert result.count == brute_force_count(Rows(), fragments)

    def test_repetitions_pad_empty_rows(self):
        fragments = [
            [{'items-0.qty': '7'}],
            [{'items--repetitions': '1'}, {'items--repetitions': '3'}],
        ]
        result = precheck(Rows(), fragments)
        assert result.count == 2
        assert result.count == brute_force_count(Rows(), fragments)

    def test_repetitions_without_rows_is_missing(self):
        # a repetition count alone never materialises the list
        result = precheck(Rows(), [[{'items--repetitions': '2'}]])
        assert result.count == 1
        assert result.form == {'items': []}

    def test_duplicate_controls_keep_values_in_order(self):
        class S(Schema):
            x = validators.Int()
        fragments = [[{'x': '1'}], [{'x': '2'}, {}]]
        # choosing both controls queues two values for one field,
        # which the form rejects; only the omission validates
        result = precheck(S(), fragments)
        assert result.count == 1
        assert result.form == {'x': 1}

    def test_nested_repeated_rows(self):
        class Sub(Schema):
            v = validators.Int(if_empty=0, if_missing=0)

        class Item2(Schema):
            qty = validators.Int(if_empty=0, if_missing=0)
            subs = ForEach(Sub)

        class S(Schema):
            items = ForEach(Item2)

        fragments = [
            [{'items-0.qty': '1', 'items-0.subs-0.v': '2'},
             {'items-0.qty': '1'}],
            [{'items-1.subs-0.v': '3'}, {}],
        ]
        result = precheck(S(), fragments)
        assert result.count == brute_force_count(S(), fragments) == 4

    def test_row_level_fields_match(self):
        class MItem(Schema):
            a = validators.Int(if_missing=0)
            b = validators.Int(if_missing=0)
            chained_validators = [validators.FieldsMatch('a', 'b')]

        class S(Schema):
            items = ForEach(MItem)

        fragments = [
            [{'items-0.a': '1'}, {'items-0.a': '2'}],
            [{'items-0.b': '1'}, {'items-0.b': '2'}],
        ]
        assert precheck(S(), fragments).count == 2

    def test_many_fragments_not_enumerated(self):
        # 250 independent ambiguous rows: 2**250 combinations must not
        # be enumerated to learn the count
        fragments = [
            [{'items-%d.qty' % i: '1'}, {'items-%d.qty' % i: '2'}]
            for i in range(250)
        ]
        result = precheck(Rows(), fragments)
        assert result.count == 2 ** 250
        assert result.field == 'items'
        first, second = result.choices
        form_a = build_form(Rows(), fragments, first)
        form_b = build_form(Rows(), fragments, second)
        assert form_a['items'] != form_b['items']

    def test_identical_rows_collapse(self):
        # every row scans to the same content, so the 2**300
        # combinations yield only the 301 distinct list lengths
        fragments = [
            [{'items-%d.qty' % i: '1'}, {}] for i in range(300)
        ]
        result = precheck(Rows(), fragments)
        assert result.count == 301

    def test_shifting_fragment_with_neighbour(self):
        # one fragment is unsure between rows 1 and 2, another fills
        # row 2 with the same content: two combinations decode to the
        # same form and are counted once
        fragments = [
            [{'items-1.qty': '5'}, {'items-2.qty': '5'}],
            [{'items-2.qty': '5'}, {}],
        ]
        result = precheck(Rows(), fragments)
        assert result.count == brute_force_count(Rows(), fragments) == 2


class TestSchemaVariants:

    def test_nested_variables_pre_validator(self):
        class S(Schema):
            pre_validators = [NestedVariables()]
            items = ForEach(Item)

        fragments = [
            [{'items-0.qty': '1'}, {'items-1.qty': '1'}],
            [{'items--repetitions': '2'}, {}],
        ]
        result = precheck(S(), fragments)
        assert result.count == 2
        if result.count == 1:
            assert result.form == S().to_python(
                variable_decode({'items-0.qty': '1'}))

    def test_ignore_key_missing(self):
        class S(Schema):
            ignore_key_missing = True
            a = validators.Int()
        result = precheck(S(), [[{}]])
        assert result.count == 1
        assert result.form == {}

    def test_if_key_missing(self):
        class S(Schema):
            allow_extra_fields = True
            if_key_missing = '7'
            a = validators.Int()
        result = precheck(S(), [[{'b': '1'}]])
        assert result.count == 1
        assert result.form == {'a': 7, 'b': '1'}

    def test_oneof_and_int_and_string(self):
        class S(Schema):
            code = validators.OneOf(['x', 'y'])
            qty = validators.Int()
            name = validators.String(if_missing='n/a')
        fragments = [
            [{'code': 'x'}, {'code': 'y'}, {'code': 'z'}],
            [{'qty': '1'}, {'qty': '2'}],
        ]
        result = precheck(S(), fragments)
        assert result.count == brute_force_count(S(), fragments) == 4

    def test_readings_as_pair_lists(self):
        class S(Schema):
            a = validators.Int()
        # readings may be pair sequences, and a reading may contain
        # several controls
        fragments = [
            [[('a', '1')], [('a', '2')]],
            [[('a', '1'), ('a', '2')], []],
        ]
        # the second fragment's first reading always queues two values
        # for one field, which the form rejects; only its empty
        # reading validates, leaving the two forms of the first
        # fragment
        assert precheck(S(), fragments).count == 2

    def test_reading_with_duplicate_controls_only(self):
        class S(Schema):
            a = validators.Int()
        fragments = [
            [[('a', '1')], [('a', '2')]],
            [[('a', '1'), ('a', '2')]],
        ]
        # every combination queues at least two values for 'a'
        assert precheck(S(), fragments).count == 0


class TestAgainstBruteForce:

    def test_random_small_forms(self):
        import random
        names = ['items-0.qty', 'items-1.qty', 'items-2.qty',
                 'items--repetitions', 'a', 'b', 'qty']
        values = ['', '0', '1', '5', 'zz']

        class S(Schema):
            a = validators.Int(if_missing=0)
            b = validators.Int(if_missing=0)
            qty = validators.Int(if_empty=0, if_missing=0)
            chained_validators = [validators.FieldsMatch('a', 'b')]

        random.seed(1234)
        for _trial in range(200):
            schema = random.choice([Rows(), S()])
            fragments = []
            for _ in range(random.randint(1, 5)):
                readings = []
                for _ in range(random.randint(1, 3)):
                    reading = []
                    for _ in range(random.randint(0, 2)):
                        reading.append((random.choice(names),
                                        random.choice(values)))
                    readings.append(reading)
                fragments.append(readings)
            assert precheck(schema, fragments).count == \
                brute_force_count(schema, fragments)


def test_result_repr_and_equality():
    result = PreCheckResult(1, form={'a': 1})
    assert 'count=1' in repr(result)
    assert result == PreCheckResult(1, form={'a': 1})
    assert result != PreCheckResult(2)
    assert result != 'not a result'


def test_result_is_not_submitted_anywhere():
    # precheck only reads locally: it never mutates the schema or the
    # given fragments
    class S(Schema):
        a = validators.Int()
    fragments = [[{'a': '1'}, {'a': '2'}]]
    precheck(S(), fragments)
    assert fragments == [[{'a': '1'}, {'a': '2'}]]
