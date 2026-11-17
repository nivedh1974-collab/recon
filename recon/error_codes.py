"""UPI response codes used in the mock data.

Every code and meaning below is copied from NPCI's "UPI Error and Response Codes"
v2.9 document (saved in Drive: Recon/Data/Error codes). BD = business decline
(customer side, money never leaves). TD = technical decline (system side).
"""

# Money never leaves the payer's account.
BUSINESS_DECLINES = {
    "ZM": "INVALID MPIN",
    "Z9": "INSUFFICIENT FUNDS IN CUSTOMER (REMITTER) ACCOUNT",
    "Z8": "PER TRANSACTION LIMIT EXCEEDED AS SET BY REMITTING MEMBER",
    "U16": "RISK THRESHOLD EXCEEDED",
    "ZA": "TRANSACTION DECLINED BY CUSTOMER",
}

# System fails before the debit happens. Money never leaves.
TECH_DECLINES_BEFORE_DEBIT = {
    "XY": "REMITTER CBS OFFLINE",
    "U90": "REMITTER BANK DEEMED HIGH RESPONSE TIME CHECK DECLINE",
}

# Debit happened, credit failed. These become debit reversals and, if the
# reversal is late, customer complaints. This is where Recon earns its keep.
TECH_DECLINES_AFTER_DEBIT = {
    "U68": "CREDIT TIMEOUT",
    "BT": "ACQUIRER/BENEFICIARY UNAVAILABLE(TIMEOUT)",
    "Y1": "BENEFICIARY CBS OFFLINE",
    "U91": "BENEFICIARY BANK DEEMED HIGH RESPONSE TIME CHECK DECLINE",
}

# Beneficiary account blocked: debited, not credited (see complaint C004).
BENEFICIARY_FROZEN = {"YF": "BENEFICIARY ACCOUNT BLOCKED/FROZEN"}

# Reversal itself timed out: money stays stuck.
REVERSAL_FAILED = {"RR": "DEBIT REVERSAL TIMEOUT(REVERSAL)"}

ALL_CODES = {
    **BUSINESS_DECLINES,
    **TECH_DECLINES_BEFORE_DEBIT,
    **TECH_DECLINES_AFTER_DEBIT,
    **BENEFICIARY_FROZEN,
    **REVERSAL_FAILED,
}
