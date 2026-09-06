"""Static profile of the synthetic company and the per-period anomaly casting.

Brightline Analytics is a small B2B SaaS business. All amounts are floats of
currency units. CASTING below is the single place that decides which vendor
carries which planted anomaly in each period.
"""

PERIODS = ["2026-01", "2026-02", "2026-03"]

APPROVAL_LIMIT = 5000.00  # invoices at or above this need manager approval

CASH = "1000"
AR = "1100"
AP = "2000"
ACCRUED = "2100"
REVENUE = "4000"
FEES_EXPENSE = "6700"
BANK_FEES = "6900"
INTEREST_INCOME = "7100"

ACCOUNTS = [
    (CASH, "Cash - Operating", "asset"),
    (AR, "Accounts Receivable", "asset"),
    (AP, "Accounts Payable", "liability"),
    (ACCRUED, "Accrued Liabilities", "liability"),
    (REVENUE, "Subscription Revenue", "revenue"),
    ("6000", "Payroll and Benefits", "expense"),
    ("6100", "Rent and Facilities", "expense"),
    ("6200", "Software Subscriptions", "expense"),
    ("6300", "Cloud Hosting", "expense"),
    ("6400", "Marketing", "expense"),
    ("6500", "Office and Supplies", "expense"),
    ("6600", "Professional Services", "expense"),
    (FEES_EXPENSE, "Payment Processing Fees", "expense"),
    (BANK_FEES, "Bank Fees", "expense"),
    (INTEREST_INCOME, "Interest Income", "revenue"),
]

# vendor name -> (short code, payment terms, expense account, bank descriptor prefix)
VENDORS = {
    "Amazon Web Services": ("AWS", "net15", "6300", "AMZN WEB SERV"),
    "WeWork": ("WW", "net5", "6100", "WEWORK COMMONS"),
    "Gusto": ("GUS", "due_on_receipt", "6000", "GUSTO PAYROLL"),
    "Datadog": ("DD", "net15", "6200", "DATADOG INC"),
    "Slack Technologies": ("SLK", "net15", "6200", "SLACK TECH"),
    "Google Workspace": ("GWS", "net15", "6200", "GOOGLE WORKSPACE"),
    "GitHub": ("GH", "net15", "6200", "GITHUB INC"),
    "Figma": ("FIG", "net15", "6200", "FIGMA MONTHLY"),
    "Notion Labs": ("NO", "net15", "6200", "NOTION LABS"),
    "Zoom Video Communications": ("ZM", "net15", "6200", "ZOOM.US"),
    "HubSpot": ("HS", "net30", "6400", "HUBSPOT INC"),
    "Mailchimp": ("MC", "net15", "6400", "MAILCHIMP"),
    "Salesforce": ("SF", "net30", "6200", "SALESFORCE COM"),
    "Twilio": ("TW", "net15", "6200", "TWILIO"),
    "Atlassian": ("ATL", "net30", "6200", "ATLASSIAN"),
    "Staples": ("STP", "net15", "6500", "STAPLES 00"),
    "Uline": ("UL", "net15", "6500", "ULINE SHIP SPLY"),
    "Cooley LLP": ("CL", "net30", "6600", "COOLEY LLP"),
}

# vendor name -> (expected monthly amount, expected day of month)
RECURRING = {
    "WeWork": (12450.00, 1),
    "Amazon Web Services": (9100.00, 3),
    "Datadog": (2350.00, 5),
    "Slack Technologies": (1240.00, 8),
    "Google Workspace": (890.00, 10),
    "Gusto": (62400.00, 15),
}

# fraction the actual bill may drift from the expected amount; rent is contractual
RECURRING_JITTER = {
    "WeWork": 0.0,
    "Amazon Web Services": 0.08,
    "Datadog": 0.02,
    "Slack Technologies": 0.015,
    "Google Workspace": 0.01,
    "Gusto": 0.03,
}

# recurring payments whose bank line clears a few days after the GL cash posting
FUZZY_BANK_OFFSET = {"WeWork": 2, "Google Workspace": 1}

# one-off monthly bills: vendor name -> (base amount, jitter fraction)
ONE_OFF_BILLS = {
    "GitHub": (420.00, 0.05),
    "Figma": (675.00, 0.05),
    "Notion Labs": (380.00, 0.04),
    "Zoom Video Communications": (519.35, 0.02),
    "HubSpot": (1580.00, 0.06),
    "Mailchimp": (640.00, 0.05),
    "Salesforce": (3120.00, 0.03),
}

# bills recorded late in the month and still unpaid at month end
OPEN_INVOICE_BILLS = {
    "Atlassian": (2860.00, 0.05),
    "Uline": (1150.00, 0.10),
}

DODO_PAYOUTS_PER_MONTH = 4
DODO_FEE_RATE = 0.032

# which vendor carries which planted anomaly, per period
CASTING = {
    "2026-01": {
        "missing_recurring": "Google Workspace",
        "vendor_variant": ("Datadog", "Datadog, Inc."),
        "duplicate_vendor": "HubSpot",
        "split_vendor": "Cooley LLP",
        "one_to_many": ("Twilio", 3),
        "timing_vendor": "Figma",
        "out_of_period_date": "2026-02-03",
        "rni_vendor": "Uline",
    },
    "2026-02": {
        "missing_recurring": "Slack Technologies",
        "vendor_variant": ("Amazon Web Services", "Amazon Web Services Inc"),
        "duplicate_vendor": "Figma",
        "split_vendor": "Mailchimp",
        "one_to_many": ("Staples", 2),
        "timing_vendor": "Zoom Video Communications",
        "out_of_period_date": "2026-01-29",
        "rni_vendor": "Staples",
    },
    "2026-03": {
        "missing_recurring": "Datadog",
        "vendor_variant": ("Twilio", "Twilio Inc."),
        "duplicate_vendor": "Atlassian",
        "split_vendor": "Cooley LLP",
        "one_to_many": ("Twilio", 2),
        "timing_vendor": "GitHub",
        "out_of_period_date": "2026-04-02",
        "rni_vendor": "Uline",
    },
}
