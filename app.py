
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import time

import pandas as pd
import streamlit as st


# =========================================================
# CONFIGURATION
# =========================================================

SOURCE_URL = "https://beta.bseindia.com/corporates/corporates_act.html"

MIN_DIVIDEND_YIELD = 3.0
MIN_EX_DATE_DAYS = 7
MIN_CURRENT_PRICE = 100.0
QUOTE_LIMIT = 150

st.set_page_config(
    page_title="BSE Corporate Actions Dashboard",
    page_icon="📈",
    layout="wide",
)


# =========================================================
# HELPERS
# =========================================================

def get_value(row, *keys, default=None):
    """Return the first non-empty value from possible field names."""
    for key in keys:
        if key in row:
            value = row[key]
            if value is not None and not pd.isna(value):
                if str(value).strip():
                    return value
    return default


def parse_date(value):
    if value is None:
        return pd.NaT

    try:
        if pd.isna(value) or not str(value).strip():
            return pd.NaT
    except (TypeError, ValueError):
        return pd.NaT

    return pd.to_datetime(value, errors="coerce", dayfirst=True)


def classify(purpose):
    """Identify the corporate action from its purpose text."""
    p = str(purpose or "").lower().strip()

    if "bonus" in p:
        return "Bonus"

    if "dividend" in p:
        return "Dividend"

    split_terms = (
        "stock split",
        "split from",
        "split of",
        "sub-division",
        "sub division",
        "subdivision",
    )

    if any(term in p for term in split_terms):
        return "Stock Split"

    return None


def dividend_amount(purpose):
    """
    Extract an explicitly stated rupee-per-share amount.
    Returns None when the amount cannot be identified.
    """
    p = str(purpose or "").lower().replace(",", "")

    patterns = (
        r"rs\.?\s*[-:]?\s*(\d+(?:\.\d+)?)",
        r"₹\s*(\d+(?:\.\d+)?)",
        r"rupees?\s*(\d+(?:\.\d+)?)",
    )

    for pattern in patterns:
        match = re.search(pattern, p)
        if match:
            return float(match.group(1))

    return None


# =========================================================
# FETCH BSE CORPORATE ACTIONS
# =========================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_actions(start_iso, end_iso):
    from bse import BSE

    folder = Path(".bse_downloads")
    folder.mkdir(exist_ok=True)

    with BSE(download_folder=folder) as client:
        return client.actions(
            segment="equity",
            from_date=datetime.fromisoformat(start_iso),
            to_date=datetime.fromisoformat(end_iso),
            by_date="ex",
        )


def normalise(records):
    """Convert BSE results into a consistent DataFrame."""

    if isinstance(records, pd.DataFrame):
        records = records.to_dict(orient="records")
    elif records is None:
        records = []
    elif isinstance(records, dict):
        records = [records]

    rows = []

    for r in records:
        if not isinstance(r, dict):
            continue

        purpose = str(
            get_value(
                r,
                "Purpose",
                "purpose",
                "purpose_desc",
                default="",
            ) or ""
        ).strip()

        category = classify(purpose)

        if category is None:
            continue

        code = get_value(
            r,
            "scrip_code",
            "SCRIP_CD",
            "Security Code",
            "security_code",
            "scripcode",
        )

        name = get_value(
            r,
            "long_name",
            "company_name",
            "Security Name",
            "security_name",
            "short_name",
            default="",
        )

        ex_date = parse_date(
            get_value(
                r,
                "Ex_date",
                "exdate",
                "EX_DATE",
                "Ex Date",
                "ex_date",
            )
        )

        rows.append({
            "Category": category,
            "Security Code": (
                str(code).strip() if code is not None else ""
            ),
            "Security Name": str(name or ""),
            "Ex Date": ex_date,
            "Purpose": purpose,
            "_Dividend Amount": (
                dividend_amount(purpose)
                if category == "Dividend"
                else None
            ),
        })

    columns = [
        "Category",
        "Security Code",
        "Security Name",
        "Ex Date",
        "Purpose",
        "_Dividend Amount",
    ]

    df = pd.DataFrame(rows, columns=columns)

    if not df.empty:
        df = df.drop_duplicates(
            subset=[
                "Category",
                "Security Code",
                "Ex Date",
                "Purpose",
            ]
        )

        df = df.sort_values(
            ["Ex Date", "Security Name"],
            na_position="last",
        )

    return df.reset_index(drop=True)


# =========================================================
# FETCH CURRENT PRICE
# =========================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_price(code):
    from bse import BSE

    folder = Path(".bse_downloads")
    folder.mkdir(exist_ok=True)

    with BSE(download_folder=folder) as client:
        quote = client.quote(str(code))

    if not isinstance(quote, dict):
        return None

    for key in (
        "LTP",
        "ltp",
        "LastPrice",
        "last_price",
        "CurrentPrice",
    ):
        try:
            value = quote.get(key)

            if value not in (None, ""):
                price = float(value)

                if price > 0:
                    return price

        except (TypeError, ValueError):
            continue

    return None


# =========================================================
# CALCULATE PRICE AND DIVIDEND YIELD
# =========================================================

def enrich(actions, history):
    out = actions.copy()

    if out.empty:
        out["Current Price"] = pd.Series(dtype=float)
        out["Dividend Yield (%)"] = pd.Series(dtype=float)
        return out

    codes = [
        code
        for code in out["Security Code"].dropna().astype(str).unique()
        if code.strip()
    ]

    prices = {}

    # Limit the number of individual quote requests.
    for i, code in enumerate(codes[:QUOTE_LIMIT]):
        try:
            prices[code] = fetch_price(code)
        except Exception:
            prices[code] = None

        if i < min(len(codes), QUOTE_LIMIT) - 1:
            time.sleep(0.08)

    out["Current Price"] = out["Security Code"].map(prices)

    # Calculate annual dividend per share from identifiable
    # dividend records in the previous 365 days.
    totals = {}

    if not history.empty:
        h = history[
            history["Category"] == "Dividend"
        ].copy()

        h["_Dividend Amount"] = pd.to_numeric(
            h["_Dividend Amount"],
            errors="coerce",
        )

        h = h.dropna(subset=["_Dividend Amount"])

        totals = h.groupby("Security Code")[
            "_Dividend Amount"
        ].sum().to_dict()

    def calculate_yield(row):
        if row["Category"] != "Dividend":
            return None

        price = row["Current Price"]

        if pd.isna(price) or price <= 0:
            return None

        annual_dividend = totals.get(
            str(row["Security Code"])
        )

        if annual_dividend is None:
            return None

        return round(
            float(annual_dividend) / float(price) * 100,
            2,
        )

    out["Dividend Yield (%)"] = out.apply(
        calculate_yield,
        axis=1,
    )

    return out


# =========================================================
# RULE ENGINE
# =========================================================

def evaluate_security(row):
    """
    Return BUY, SELL or NEEDS REVIEW.

    Dividend:
      Yield > 3%
      Ex-date more than 7 days away
      Price > Rs. 100

    Stock Split / Bonus:
      Ex-date more than 7 days away
      Price > Rs. 100
    """

    category = row["Category"]
    price = row["Current Price"]
    ex_date = row["Ex Date"]
    yield_value = row["Dividend Yield (%)"]

    checks = {}

    # Check current price.
    if pd.isna(price) or price <= 0:
        checks["Price"] = None
    else:
        checks["Price"] = price > MIN_CURRENT_PRICE

    # Check ex-date.
    if pd.isna(ex_date):
        checks["Ex-date"] = None
    else:
        days_until_ex_date = (
            pd.Timestamp(ex_date).date() - date.today()
        ).days

        checks["Ex-date"] = (
            days_until_ex_date > MIN_EX_DATE_DAYS
        )

    # Dividend yield is required only for dividends.
    if category == "Dividend":
        if pd.isna(yield_value):
            checks["Dividend yield"] = None
        else:
            checks["Dividend yield"] = (
                yield_value > MIN_DIVIDEND_YIELD
            )

    # Missing required information takes precedence.
    if any(value is None for value in checks.values()):
        status = "NEEDS REVIEW"

    elif all(checks.values()):
        status = "BUY"

    else:
        status = "SELL"

    failed = [
        name
        for name, result in checks.items()
        if result is False
    ]

    missing = [
        name
        for name, result in checks.items()
        if result is None
    ]

    if missing:
        reason = "Missing data: " + ", ".join(missing)
    elif failed:
        reason = "Failed: " + ", ".join(failed)
    else:
        reason = "All applicable rules passed"

    return pd.Series({
        "Screening Status": status,
        "Screening Reason": reason,
    })


def apply_screening(data):
    if data.empty:
        data = data.copy()
        data["Screening Status"] = pd.Series(dtype=str)
        data["Screening Reason"] = pd.Series(dtype=str)
        return data

    results = data.apply(evaluate_security, axis=1)

    return pd.concat(
        [data.reset_index(drop=True), results.reset_index(drop=True)],
        axis=1,
    )


# =========================================================
# TABLE RENDERING
# =========================================================

def render_table(data, category):
    st.subheader(category)

    part = data[data["Category"] == category].copy()

    if part.empty:
        st.info(
            f"No {category.lower()} records found for this date range."
        )
        return

    if category == "Dividend":
        columns = [
            "Security Code",
            "Security Name",
            "Current Price",
            "Dividend Yield (%)",
            "Purpose",
            "Ex Date",
            "Screening Status",
            "Screening Reason",
        ]
    else:
        columns = [
            "Security Code",
            "Security Name",
            "Current Price",
            "Purpose",
            "Ex Date",
            "Screening Status",
            "Screening Reason",
        ]

    # Keep dates as dates so header sorting is chronological.
    part = part[columns]

    st.dataframe(
        part,
        use_container_width=True,
        hide_index=True,
        height=min(560, 40 + 35 * len(part)),
        column_config={
            "Security Code": st.column_config.TextColumn(
                "Security Code"
            ),
            "Security Name": st.column_config.TextColumn(
                "Security Name",
                width="medium",
            ),
            "Current Price": st.column_config.NumberColumn(
                "Current Price (₹)",
                format="₹%.2f",
            ),
            "Dividend Yield (%)": st.column_config.NumberColumn(
                "Dividend Yield (%)",
                format="%.2f%%",
            ),
            "Purpose": st.column_config.TextColumn(
                "Purpose",
                width="large",
            ),
            "Ex Date": st.column_config.DateColumn(
                "Ex Date",
                format="DD-MMM-YYYY",
            ),
            "Screening Status": st.column_config.TextColumn(
                "Screening Status",
                width="medium",
            ),
            "Screening Reason": st.column_config.TextColumn(
                "Screening Reason",
                width="large",
            ),
        },
    )

    st.caption("Click a column header to sort the table.")


# =========================================================
# PAGE HEADER AND SIDEBAR
# =========================================================

st.title("📈 BSE Corporate Actions Dashboard")

st.caption(
    "Corporate actions, market prices and rule-based screening"
)

with st.sidebar:
    st.header("Controls")

    today = date.today()

    start = st.date_input(
        "Ex-date from",
        today,
    )

    end = st.date_input(
        "Ex-date to",
        today + timedelta(days=365),
    )

    st.markdown(f"[Open BSE source page]({SOURCE_URL})")

    st.caption(
        "Quotes may be delayed or unavailable. "
        "Always verify market data before investing."
    )

    if st.button(
        "↻ Refresh BSE data",
        type="primary",
        use_container_width=True,
    ):
        st.cache_data.clear()
        st.rerun()


if start > end:
    st.error("Start date must be on or before end date.")
    st.stop()


# =========================================================
# FETCH CORPORATE ACTIONS
# =========================================================

today = date.today()

try:
    with st.spinner("Fetching corporate actions from BSE…"):
        actions = normalise(
            fetch_actions(
                datetime.combine(
                    start,
                    datetime.min.time(),
                ).isoformat(),
                datetime.combine(
                    end,
                    datetime.max.time(),
                ).isoformat(),
            )
        )

        history = normalise(
            fetch_actions(
                datetime.combine(
                    today - timedelta(days=365),
                    datetime.min.time(),
                ).isoformat(),
                datetime.combine(
                    today,
                    datetime.max.time(),
                ).isoformat(),
            )
        )

except Exception as e:
    st.error(
        "Could not fetch BSE data. The source may be unavailable, "
        "may have changed, or may be limiting requests."
    )

    st.markdown(
        f"[Check the BSE corporate actions page]({SOURCE_URL})"
    )

    with st.expander("Technical details"):
        st.code(f"{type(e).__name__}: {e}")

    st.stop()


if actions.empty:
    st.warning(
        "No matching stock split, bonus or dividend records "
        "were returned for this ex-date range."
    )
    st.stop()


# =========================================================
# ENRICH AND SCREEN
# =========================================================

with st.spinner("Fetching prices and applying screening rules…"):
    data = enrich(actions, history)
    data = apply_screening(data)


# =========================================================
# SUMMARY METRICS
# =========================================================

buy_count = int((data["Screening Status"] == "BUY").sum())
sell_count = int((data["Screening Status"] == "SELL").sum())
review_count = int(
    (data["Screening Status"] == "NEEDS REVIEW").sum()
)

c1, c2, c3, c4 = st.columns(4)

c1.metric("BUY candidates", buy_count)
c2.metric("SELL", sell_count)
c3.metric("Needs review", review_count)
c4.metric("Securities", len(data))

st.caption(
    "Screening is rule-based. BUY candidates qualify for further "
    "research; they are not guaranteed investment recommendations."
)


# =========================================================
# SEARCH
# =========================================================

query = st.text_input(
    "Search securities",
    placeholder="Security code, company name or purpose",
)

if query.strip():
    mask = (
        data["Security Code"].astype(str).str.contains(
            query,
            case=False,
            na=False,
        )
        | data["Security Name"].astype(str).str.contains(
            query,
            case=False,
            na=False,
        )
        | data["Purpose"].astype(str).str.contains(
            query,
            case=False,
            na=False,
        )
    )

    data = data[mask].copy()


# =========================================================
# TOP PICKS / BUY CANDIDATES
# =========================================================

st.markdown("---")
st.subheader("🏆 Top Picks — BUY Candidates")

st.caption(
    "Securities that pass every applicable rule. "
    "These are screening candidates, not investment advice."
)

top_picks = data[
    data["Screening Status"] == "BUY"
].copy()

if top_picks.empty:
    st.info(
        "No securities currently pass all applicable rules "
        "for this date range and search."
    )
else:
    # Dividend candidates: highest yield first.
    # Stock splits and bonus issues: nearest eligible ex-date first.
    top_picks["_Category Order"] = (
        top_picks["Category"]
        .map({
            "Dividend": 0,
            "Stock Split": 1,
            "Bonus": 2,
        })
        .fillna(3)
    )

    top_picks["_Yield Sort"] = top_picks[
        "Dividend Yield (%)"
    ].fillna(-1)

    top_picks = top_picks.sort_values(
        ["_Category Order", "_Yield Sort", "Ex Date"],
        ascending=[True, False, True],
        na_position="last",
    )

    pick_columns = [
        "Security Code",
        "Security Name",
        "Category",
        "Current Price",
        "Dividend Yield (%)",
        "Ex Date",
        "Purpose",
    ]

    top_display = top_picks[pick_columns].copy()

    st.dataframe(
        top_display,
        use_container_width=True,
        hide_index=True,
        height=min(300, 40 + 35 * len(top_display)),
        column_config={
            "Security Code": st.column_config.TextColumn(
                "Security Code"
            ),
            "Security Name": st.column_config.TextColumn(
                "Security Name",
                width="medium",
            ),
            "Category": st.column_config.TextColumn(
                "Corporate Action"
            ),
            "Current Price": st.column_config.NumberColumn(
                "Current Price (₹)",
                format="₹%.2f",
            ),
            "Dividend Yield (%)": st.column_config.NumberColumn(
                "Dividend Yield (%)",
                format="%.2f%%",
            ),
            "Ex Date": st.column_config.DateColumn(
                "Ex Date",
                format="DD-MMM-YYYY",
            ),
            "Purpose": st.column_config.TextColumn(
                "Purpose",
                width="large",
            ),
        },
    )


# =========================================================
# CORPORATE ACTION TABLES
# =========================================================

st.markdown("---")

tab_split, tab_bonus, tab_dividend = st.tabs(
    ["Stock Split", "Bonus", "Dividend"]
)

with tab_split:
    render_table(data, "Stock Split")

with tab_bonus:
    render_table(data, "Bonus")

with tab_dividend:
    render_table(data, "Dividend")


# =========================================================
# CSV EXPORT
# =========================================================

download = data.drop(
    columns=["_Dividend Amount"],
    errors="ignore",
).copy()

download["Ex Date"] = pd.to_datetime(
    download["Ex Date"],
    errors="coerce",
).dt.strftime("%Y-%m-%d")

st.markdown("---")

st.download_button(
    "⬇ Download filtered data (CSV)",
    download.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"bse_corporate_actions_{today.isoformat()}.csv",
    mime="text/csv",
)


# =========================================================
# FOOTER
# =========================================================

st.caption(
    "Dividend yield is estimated using identifiable dividend "
    "amounts in the previous 365 days divided by the current quote. "
    "Unparseable or missing data produces Needs Review."
)

st.caption(
    "BSE source data and quotes may be incomplete or delayed. "
    "Verify corporate actions, prices and dividend information "
    "before making investment decisions."
)
