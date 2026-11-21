import pyexcel as p

from .nose_tools import eq_


def test_a_dictionary_of_sheet():
    test_data = [["a", "b"]]

    book_dict = {"test": p.Sheet(test_data)}

    book = p.Book(book_dict)
    eq_(book.test.array, test_data)


def test_book_len():
    test_data = [["a", "b"]]

    book_dict = {"test": p.Sheet(test_data)}

    book = p.Book(book_dict)
    eq_(len(book.test.array), 1)


def test_sheet_ordering():
    test_data = [["a", "b"]]

    book_dict = {"first": test_data, "middle": test_data, "last": test_data}

    book = p.Book(book_dict)
    eq_(book.sheet_names(), ["first", "middle", "last"])


def _make_source_books():
    book_a = p.Book({"Sales": [["a_sales"]], "Sales_1": [["a_sales_1"]]})
    book_b = p.Book({"Sales": [["b_sales"]], "Sales_2": [["b_sales_2"]]})
    book_c = p.Book({"Sales_3": [["c_sales_3"]], "Other": [["c_other"]]})
    return book_a, book_b, book_c


def _signature(book):
    return [(name, book[name].array) for name in book.sheet_names()]


def test_book_merge_names_are_deterministic():
    # merging unrelated books first must not influence later merges
    for _ in range(2):
        left = p.Book({"Dup": [["x"]], "Dup_1": [["y"]]})
        left + p.Book({"Dup": [["z"]]})

    book_a, book_b, book_c = _make_source_books()
    result1 = (book_a + book_b) + book_c

    book_a, book_b, book_c = _make_source_books()
    result2 = book_a + (book_b + book_c)

    book_a, book_b, book_c = _make_source_books()
    result3 = p.Book(book_a.to_dict())
    result3 += book_b
    result3 += book_c

    expected = ["Sales", "Sales_1", "Sales_4", "Sales_2", "Sales_3", "Other"]
    eq_(result1.sheet_names(), expected)
    eq_(result2.sheet_names(), expected)
    eq_(result3.sheet_names(), expected)
    eq_(_signature(result1), _signature(result2))
    eq_(_signature(result1), _signature(result3))

    eq_(result1["Sales"].array, [["a_sales"]])
    eq_(result1["Sales_1"].array, [["a_sales_1"]])
    eq_(result1["Sales_4"].array, [["b_sales"]])
    eq_(result1["Sales_2"].array, [["b_sales_2"]])
    eq_(result1["Sales_3"].array, [["c_sales_3"]])
    eq_(result1["Other"].array, [["c_other"]])


def test_book_merge_genuine_names_win_over_auto_ones():
    book_a, book_b, book_c = _make_source_books()
    temp = book_a + book_b
    # the temporary book can be queried by its then-current names
    eq_(temp.sheet_names(), ["Sales", "Sales_1", "Sales_3", "Sales_2"])
    eq_(temp["Sales_3"].array, [["b_sales"]])
    # in the final book, the genuine name Sales_3 belongs to book_c's
    # sheet, and book_b's Sales takes the still free name Sales_4
    result = temp + book_c
    eq_(result["Sales_3"].array, [["c_sales_3"]])
    eq_(result["Sales_4"].array, [["b_sales"]])


def test_book_merge_keeps_source_data_independent():
    book_a, book_b, book_c = _make_source_books()
    temp = book_a + book_b
    temp["Sales_3"].row[0] = [["mutated"]]
    eq_(book_b["Sales"].array, [["b_sales"]])
    result = temp + book_c
    result["Sales"].row[0] = [["mutated"]]
    result["Sales_4"].row[0] = [["mutated"]]
    result["Sales_3"].row[0] = [["mutated"]]
    eq_(book_a["Sales"].array, [["a_sales"]])
    eq_(book_b["Sales"].array, [["b_sales"]])
    eq_(book_c["Sales_3"].array, [["c_sales_3"]])


def test_renamed_sheet_is_treated_as_a_genuine_name():
    book_a, book_b, book_c = _make_source_books()
    result = (book_a + book_b) + book_c
    result["Sales_4"].name = "Report"
    eq_(
        result.sheet_names(),
        ["Sales", "Sales_1", "Report", "Sales_2", "Sales_3", "Other"],
    )
    eq_(result["Report"].array, [["b_sales"]])
    eq_(result.Report.array, [["b_sales"]])

    book_d = p.Book({"Report": [["d_report"]], "Extra": [["d_extra"]]})
    merged = result + book_d
    eq_(
        merged.sheet_names(),
        [
            "Sales",
            "Sales_1",
            "Report",
            "Sales_2",
            "Sales_3",
            "Other",
            "Report_1",
            "Extra",
        ],
    )
    eq_(merged["Report"].array, [["b_sales"]])
    eq_(merged["Report_1"].array, [["d_report"]])
    # the source books stay intact
    eq_(book_b["Sales"].array, [["b_sales"]])


def test_iadd_matches_add_for_single_sheet_books():
    multi = p.Book({"Sheet1": [["m1"]], "Sheet2": [["m2"]]})
    single = p.Book({"Sheet 1": [["only"]]}, filename="single.xls")
    merged = multi + single
    eq_(merged.sheet_names(), ["Sheet1", "Sheet2", "single.xls_Sheet 1"])

    in_place = p.Book(multi.to_dict())
    in_place += single
    eq_(in_place.sheet_names(), merged.sheet_names())
