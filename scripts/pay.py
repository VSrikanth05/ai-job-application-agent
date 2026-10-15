"""Salary parsing + the user's pay rules (config `pay:`), shared by filter_jobs and score_jobs.

    parse_salary("$80K/yr - $100K/yr")      -> Salary(currency="USD", annual=90000.0, annual_usd=90000.0, period="year")
    parse_salary("₹6L/yr - ₹10L/yr")        -> Salary(currency="INR", ...)
    pay_problem(salary_text, cfg)           -> None (fine / not stated) or a short reason

Rules (user's choice): pay must be in an allowed currency (USD, EUR, GBP, CHF, CAD, AUD, SGD, NZD) and,
when an amount is stated, at least `min_annual_usd` a year (midpoint of a range, converted with the
approximate rates in config; an amount with no currency is checked as USD). Unstated pay is decided later
by the scorer (employer outside India only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Currency prefixes, longest first, and never inside a word: "US$" must not read as "S$", "R$" is BRL, not USD.
SYMBOLS = [("CA$", "CAD"), ("AU$", "AUD"), ("SG$", "SGD"), ("NZ$", "NZD"), ("US$", "USD"), ("HK$", "HKD"),
           ("NT$", "TWD"), ("MX$", "MXN"), ("R$", "BRL"), ("C$", "CAD"), ("A$", "AUD"), ("S$", "SGD"),
           ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("¥", "JPY"), ("$", "USD")]
SYMBOL_RE = [(re.compile(r"(?<![A-Za-z])" + re.escape(sym)), code) for sym, code in SYMBOLS]
CODES = {"USD", "EUR", "GBP", "CHF", "CAD", "AUD", "SGD", "NZD", "INR", "JPY", "CNY", "PLN", "CZK", "RON",
         "HUF", "SEK", "NOK", "DKK", "BRL", "MXN", "ARS", "COP", "ZAR", "NGN", "PKR", "BDT", "LKR", "PHP",
         "IDR", "VND", "MYR", "THB", "AED", "SAR", "TRY", "UAH", "RUB", "KES", "EGP", "HKD", "TWD", "KRW", "ILS"}
CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(CODES)) + r")(?![A-Za-z])")
INR_WORDS = re.compile(r"₹|\blpa\b|\blakhs?\b|\blacs?\b|\bcrores?\b|\brs\.?\s?\d|\brupees?\b", re.I)
NUM = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|m|l|lakhs?|lacs?|lpa|cr|crores?)?(?![a-z])", re.I)
# A number followed by one of these is not money ("40 hours/week", "3+ years", "0.25%", "500 employees").
NOT_MONEY = re.compile(r"^\s*(?:\+\s*)?(?:%|percent|hours?\b|hrs?\b|h/w|days?\b|weeks?\b|wks?\b|years?\b|yrs?\b|"
                       r"months? (?:of|experience)|people|employees|members|countries|time ?zones?|x\b)", re.I)
SUFFIX = {"k": 1_000, "m": 1_000_000, "l": 100_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
          "lacs": 100_000, "lpa": 100_000, "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}
RANGE_SEP = re.compile(r"^\s*(?:-|–|—|to)\s*[^\d\s]{0,3}\s*$", re.I)
PERIODS = [("hour", re.compile(r"(?:per\s*|/\s*|an?\s+)(?:hour|hr)s?\b|\bhourly\b|/\s*h\b", re.I), 2080),
           ("day", re.compile(r"(?:per\s*|/\s*|a\s+)day\b|\bdaily\b", re.I), 260),
           ("week", re.compile(r"(?:per\s*|/\s*|a\s+)(?:week|wk)\b|\bweekly\b", re.I), 52),
           ("month", re.compile(r"(?:per\s*|/\s*|a\s+)(?:month|mo|mês|mes)\b|\bmonthly\b|/\s*m\b|\bpcm\b", re.I), 12),
           ("year", re.compile(r"(?:per\s*|/\s*|a\s+)(?:year|yr|annum)\b|\bannual(?:ly)?\b|\byearly\b|\bp\.?a\.?(?![a-z])"
                               r"|\b(?:year|yr)\b", re.I), 1)]


@dataclass
class Salary:
    currency: str | None
    annual: float | None       # in `currency`
    annual_usd: float | None
    period: str | None = None  # hour / day / week / month / year (None = not stated)


def _currency(text: str) -> str | None:
    if INR_WORDS.search(text):
        return "INR"
    hits = [(m.start(), m.group(1)) for m in CODE_RE.finditer(text.upper())]
    for rx, code in SYMBOL_RE:
        m = rx.search(text)
        if m:
            hits.append((m.start(), code))
            break  # SYMBOLS is ordered most-specific first
    if hits:
        return min(hits)[1]  # whichever marker appears first
    if re.search(r"\bfr\.?\s?\d|\bchf\b", text, re.I):
        return "CHF"
    return None


def _to_float(raw: str) -> float:
    """'35.000' / '1.200.000' (EU thousands), '45.000,50' (EU decimal), '1,20,000' (Indian), '120,000', '1.5'."""
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):          # 45.000,50
            return float(raw.replace(".", "").replace(",", "."))
        return float(raw.replace(",", ""))             # 1,500.50
    if "," in raw:
        if re.fullmatch(r"\d+,\d{1,2}", raw):          # 1500,5 (decimal comma)
            return float(raw.replace(",", "."))
        return float(raw.replace(",", ""))             # 120,000 / 1,20,000
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):      # 35.000 / 1.200.000
        return float(raw.replace(".", ""))
    return float(raw)


def _money_numbers(text: str) -> list[tuple[float, re.Match]]:
    found = []  # (value, multiplier or None, match)
    for m in NUM.finditer(text):
        if NOT_MONEY.match(text[m.end():m.end() + 25]) and not m.group(2):
            continue
        try:
            val = _to_float(m.group(1))
        except ValueError:
            continue
        found.append((val, SUFFIX.get((m.group(2) or "").lower()), m))
    out = []
    for i, (val, mult, m) in enumerate(found):
        # "40-50k" / "6-10 LPA": a range's trailing unit applies to its first number too.
        if mult is None and i + 1 < len(found):
            nval, nmult, nm = found[i + 1]
            if nmult and RANGE_SEP.match(text[m.end():nm.start()]) and val <= nval:
                mult = nmult
        val *= mult or 1
        if val >= 1:
            out.append((val, m))
    return out


def _period(text: str, start: int) -> tuple[str, int] | None:
    """The pay period word closest AFTER the amount ("$90k per year, 40 hours/week" → year); else the
    closest before it ("Annual salary: $90k")."""
    after, before = [], []
    for name, rx, mult in PERIODS:
        for m in rx.finditer(text):
            (after if m.start() >= start else before).append((abs(m.start() - start), name, mult))
    best = min(after) if after else (min(before) if before else None)
    return (best[1], best[2]) if best else None


def parse_salary(text, usd_per_unit: dict | None = None) -> Salary:
    """Best-effort: currency + yearly amount (midpoint of a range). Unknown parts come back None.
    Accepts anything (models sometimes return salary as a dict/number) and never raises."""
    if text is None:
        return Salary(None, None, None)
    text = text if isinstance(text, str) else str(text)
    text = text.strip()[:400]
    if not text:
        return Salary(None, None, None)
    cur = _currency(text)
    nums = _money_numbers(text)[:2]
    annual, period = None, None
    if nums:
        mid = sum(v for v, _ in nums) / len(nums)
        found = _period(text, nums[0][1].start())
        if found:
            period, mult = found
        else:  # no period stated: tiny numbers are hourly, otherwise a yearly figure
            period, mult = ("hour", 2080) if mid < 300 else (None, 1)
        annual = mid * mult
    rate = (usd_per_unit or {}).get(cur) if cur else None
    return Salary(cur, annual, annual * rate if (annual and rate) else None, period)


def pay_problem(text, pay_cfg: dict) -> str | None:
    """Reason the stated pay breaks the user's rules, or None (acceptable, or nothing stated)."""
    s = parse_salary(text, pay_cfg.get("usd_per_unit"))
    if s.currency and s.currency not in pay_cfg["currencies"]:
        return f"pay_currency:{s.currency}"
    usd = s.annual_usd if s.currency else s.annual  # an amount with no currency is checked as USD
    if usd is not None and usd < pay_cfg["min_annual_usd"]:
        return f"pay_too_low:~${usd:,.0f}/yr"
    return None
