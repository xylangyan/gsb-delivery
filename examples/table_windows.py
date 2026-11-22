from tableschema import WindowedTable

# A single non-rewindable device log stream: two rows land on the shared
# boundary (different offsets, same instant), then late first-window rows
# arrive before the second window gets its own next record.
SOURCE = [
    ['device_id', 'timestamp', 'warehouse_id', 'value'],
    ['d1', '2026-01-01T00:00:00Z', 'W1', 1],
    ['d2', '2026-01-01T01:00:00Z', 'W2', 2],
    ['d3', '2026-01-01T00:10:00Z', 'W1', 3],
    ['d4', '2026-01-01T00:20:00Z', 'W1', 4],
    ['d5', '2026-01-01T02:00:00Z', 'W2', 5],
]

SCHEMA = {
    'fields': [
        {'name': 'device_id', 'type': 'string',
         'constraints': {'unique': True}},
        {'name': 'timestamp', 'type': 'string'},
        {'name': 'warehouse_id', 'type': 'string'},
        {'name': 'value', 'type': 'integer'},
    ],
    'foreignKeys': [
        {'fields': 'warehouse_id',
         'reference': {'resource': 'warehouses', 'fields': 'id'}},
    ],
}

RELATIONS = {'warehouses': [{'id': 'W1'}, {'id': 'W2'}]}

# One stream read, fanned out into two left-closed/right-open windows.
table = WindowedTable(
    SOURCE,
    schema=SCHEMA,
    time_field='timestamp',
    timezone='UTC',
    buffer_size=1,  # force disk spillover for late rows of the slow window
    windows=[
        {'name': 'first', 'start': '2026-01-01T00:00:00Z',
         'end': '2026-01-01T01:00:00Z'},
        {'name': 'second', 'start': '2026-01-01T01:00:00Z',
         'end': '2026-01-02T00:00:00Z'},
    ])


def collect(store):
    def handler(exc, row_number=None, row_data=None, error_data=None):
        store.append((type(exc).__name__, row_number))
    return handler


first_errors = []
second_errors = []

# Interleaved ingestion: the first window parks after its first record while
# the second window reads fast; the first window then gets all late rows.
first_rows = table.window('first').iter(
    relations=RELATIONS, exc_handler=collect(first_errors))
second_rows = table.window('second').iter(
    relations=RELATIONS, exc_handler=collect(second_errors))

print('first window first row:', next(first_rows))
print('second window rows:', list(second_rows))
print('first window remaining rows:', list(first_rows))
print('first window integrity errors:', first_errors)
print('second window integrity errors:', second_errors)
print('unparseable timestamps:', table.window('first').parse_errors)

table.close()
