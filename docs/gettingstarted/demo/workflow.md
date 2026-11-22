---
title: Workflow Demonstration
---

# 📋 Workflow Demonstration

There are existing implementations of workflows to showcase how `bigtree` can be used!

## To Do Application
There are functions to:

- Add or remove list to To-Do application
- Add or remove item to list, default list is the 'General' list
- Prioritize a list/item by reordering them as first list/item
- Save and import To-Do application to and from an external JSON file
- Show To-Do application, which prints tree to console

```python
from bigtree import AppToDo

app = AppToDo("To Do App")
app.add_item(item_name="Homework 1", list_name="School")
app.add_item(item_name=["Milk", "Bread"], list_name="Groceries", description="Urgent")
app.add_item(item_name="Cook")
app.show()
# To Do App
# ├── School
# │   └── Homework 1
# ├── Groceries
# │   ├── Milk [description=Urgent]
# │   └── Bread [description=Urgent]
# └── General
#     └── Cook

app.save("list.json")
app2 = AppToDo.load("list.json")
```

## Calendar Application

There are functions to:

- Add or remove event from Calendar
- Find event by name, or name and date
- Display calendar, which prints events to console
- Export calendar to pandas DataFrame

```python
import datetime as dt
from bigtree import Calendar

calendar = Calendar("My Calendar")
calendar.add_event("Gym", "2023-01-01 18:00")
calendar.add_event("Dinner", "2023-01-01", date_format="%Y-%m-%d", budget=20)
calendar.add_event("Gym", "2023-01-02 18:00")
calendar.show()
# My Calendar
# 2023-01-01 00:00:00 - Dinner (budget: 20)
# 2023-01-01 18:00:00 - Gym
# 2023-01-02 18:00:00 - Gym

calendar.find_event("Gym")
# 2023-01-01 18:00:00 - Gym
# 2023-01-02 18:00:00 - Gym

calendar.delete_event("Gym", dt.date(2023, 1, 1))
calendar.show()
# My Calendar
# 2023-01-01 00:00:00 - Dinner (budget: 20)
# 2023-01-02 18:00:00 - Gym

data_calendar = calendar.to_dataframe()
data_calendar
#                              path    name        date      time  budget
# 0  /My Calendar/2023/01/01/Dinner  Dinner  2023-01-01  00:00:00    20.0
# 1     /My Calendar/2023/01/02/Gym     Gym  2023-01-02  18:00:00     NaN
```

## Product Hierarchy Application

There are functions to:

- Record department observations per business date, keyed by a stable product ID
- Select the latest complete observation per department, and use a manual decision when departments disagree
- Export a daily warehouse-loadable tree with one row per SKU
- Retain conflicts and corrections as dispute evidence
- Freeze an already-published value that loses all support, until a new decision selects a supported value

```python
from bigtree import ProductHierarchy

hierarchy = ProductHierarchy()
hierarchy.add_observation("North", "2026-03-01", "SKU7", "Outlet", "Old name")
hierarchy.add_observation("South", "2026-03-01", "SKU7", "Flagship", "Old name")
hierarchy.add_decision("SKU7", "Outlet", "2026-03-01", "2026-03-02")
hierarchy.add_observation("North", "2026-03-02", "SKU7", "Flagship", "Day 2 name")
hierarchy.add_observation("South", "2026-03-02", "SKU7", "Flagship", "Day 2 name")

first = hierarchy.export_day("2026-03-01")
first.row_by_sku("SKU7").parent
# 'Outlet'
first.tree.show()
# Catalog
# └── 2026-03-01
#     └── Outlet
#         └── SKU7

# Late correction to day 1; the published value loses support and is frozen
hierarchy.add_observation("North", "2026-03-01", "SKU7", "Flagship", "N7")
corrected = hierarchy.export_day("2026-03-01")
corrected.row_by_sku("SKU7").status
# 'frozen'
corrected.row_by_sku("SKU7").parent
# 'Outlet'

# A new decision can release the freeze
hierarchy.add_decision("SKU7", "Flagship", "2026-03-01", "2026-03-02")
released = hierarchy.export_day("2026-03-01")
released.row_by_sku("SKU7").parent
# 'Flagship'
released.row_by_sku("SKU7").name
# 'N7'
released.verify()
# []
```
