"""Synthetic fixture: an 'approved selection' turned into PO lines.
All names are fictional. Dates are day offsets from the seed date so that
overdue / at_risk / no_promise_date cases stay true whenever you seed."""

PARTS = [
    # part_id, description, category, uom
    ("FAS-001", "Hex bolt M6x30, stainless", "fasteners", "each"),
    ("FAS-002", "Self-locking nut M6", "fasteners", "each"),
    ("CON-001", "Circular connector 12-pin, sealed", "connectors", "each"),
    ("CON-002", "Ring terminal 6mm, crimp", "connectors", "each"),
    ("HAR-001", "Heat-shrink sleeve 6mm, black", "harness_consumables", "metre"),
    ("HAR-002", "Cable tie 200mm, UV stable", "harness_consumables", "each"),
    ("SEA-001", "Two-part polysulfide sealant, 250ml", "sealants", "tube"),
    ("SEA-002", "Anti-corrosion compound, 100g", "sealants", "tube"),
]

# Fictional suppliers (5)
ALDER = "Alder Fasteners Ltd"
BRIGHTLINE = "Brightline Connectors Ltd"
COBALT = "Cobalt Harness Supplies"
DUNMORE = "Dunmore Sealants Co"
EVERLANE = "Everlane Industrial Traders"

# po_number, line_no, part_id, supplier, ordered_qty, unit_price_inr,
#   need_by offset (days from seed date), promised offset or None
PO_LINES = [
    ("PO-1001", 1, "FAS-001", ALDER, 500, "12.50", -5, None),    # overdue, no promise
    ("PO-1001", 2, "FAS-002", ALDER, 500, "8.00", 10, 7),        # on track
    ("PO-1002", 1, "CON-001", BRIGHTLINE, 40, "1850.00", 14, 21),  # at_risk (promised after need-by)
    ("PO-1002", 2, "CON-002", BRIGHTLINE, 200, "6.50", 5, 5),    # on track
    ("PO-1003", 1, "HAR-001", COBALT, 300, "45.00", -3, 4),      # overdue, promised moved later
    ("PO-1003", 2, "HAR-002", COBALT, 1000, "2.20", 20, None),   # no promise date, not yet due
    ("PO-1004", 1, "SEA-001", DUNMORE, 60, "2400.00", 12, 12),   # PO-1004 total is over threshold
    ("PO-1004", 2, "SEA-002", DUNMORE, 30, "950.00", 12, 12),
    ("PO-1005", 1, "FAS-001", EVERLANE, 1000, "13.00", 30, 28),
    ("PO-1005", 2, "CON-002", EVERLANE, 500, "6.80", 30, 35),    # at_risk
    ("PO-1005", 3, "HAR-002", EVERLANE, 500, "2.30", 25, 25),
]
