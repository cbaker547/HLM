Hospital Price Explorer
Team 224 · CSE 6242 · Spring 2026
================================


DESCRIPTION
-----------
An interactive web viewer for comparing U.S. hospital prices.
Covers 934 hospitals across 49 states, 23 procedures (18 CPT +
5 DRG), and 333,203 price records from DoltHub and CMS.

Click a state on the map to see its hospitals. On the Ride side panel you can click on 
the list of hospitals for each state. You can click on a hospital to see a breakdown on procedures
of outpatient and inpatient procedures with pricing. 
commercial, cash, and Medicare prices side-by-side. Each
hospital is scored by a Hospital Markup Index (HMI) built from
a hierarchical linear model.
The graphs shows a visual representation of pricing for outpatient procedures.

The package includes the viewer, the analytical dataset, the
raw source data, and the Python pipeline that rebuilds it.


INSTALLATION
------------
  - Requires Python 3.12:
    https://www.python.org/downloads/)
  - Unzip the package. No install step needed to run the demo.


EXECUTION
---------
  1. Open the project folder.
  2. Double-click the launcher for your OS:
       macOS:    launch.command
       run  python3 launch.py  in a terminal
  3. Your browser opens at http://localhost:8765 automatically.
  4. Click a state, then a hospital, to explore prices.
  5. Press Ctrl+C in the terminal (or close it) when done.
