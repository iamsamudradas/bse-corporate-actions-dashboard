
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
# DATA HELPERS
# =========================================================

def get_value(row, *keys, default=None):
    for key in keys:
        if key not in row:
            continue

        value = row[key]

        if value is None:
            continue

        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass

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
    """Extract an explicitly stated dividend amount per share."""
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


def normalise(records):
    if isinstance(records, pd.DataFrame):
        records = records.to_dict(orient="records")
    elif records is None:
        records = []
    elif isinstance(records, dict):
        records = [records]

    rows = []

    for record in records:
        if not isinstance(record, dict):
            continue

        purpose = str(
            get_value(
                record,
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
            record,
            "scrip_code",
            "SCRIP_CD",
            "Security Code",
            "security_code",
            "scripcode",
        )

        name = get_value(
            record,
            "long_name",
            "company_name",
            "Security Name",
            "security_name",
            "short_name",
            default="",
        )

        ex_date = parse_date(
            get_value(
                record,
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
# BSE DATA FETCHING
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

    for i, code in enumerate(codes[:QUOTE_LIMIT]):
        try:
            prices[code] = fetch_price(code)
        except Exception:
            prices[code] = None

        if i < min(len(codes), QUOTE_LIMIT) - 1:
            time.sleep(0.08)

    out["Current Price"] = out["Security Code"].map(prices)

    # Estimate annual dividend yield using identifiable
    # dividend amounts in the previous 365 days.
    totals = {}

    if not history.empty:
        history_dividends = history[
            history["Category"] == "Dividend"
        ].copy()

        history_dividends["_Dividend Amount"] = pd.to_numeric(
            history_dividends["_Dividend Amount"],
            errors="coerce",
        )

        history_dividends = history_dividends.dropna(
            subset=["_Dividend Amount"]
        )

        totals = history_dividends.groupby("Security Code")[
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
    category = row["Category"]
    price = row["Current Price"]
    ex_date = row["Ex Date"]
    yield_value = row["Dividend Yield (%)"]

    checks = {}

    # Price rule applies to all three corporate action types.
    if pd.isna(price) or price <= 0:
        checks["Current price"] = None
    else:
        checks["Current price"] = price > MIN_CURRENT_PRICE

    # Ex-date rule applies to all three corporate action types.
    if pd.isna(ex_date):
        checks["Ex-date"] = None
    else:
        days_left = (
            pd.Timestamp(ex_date).date() - date.today()
        ).days

        checks["Ex-date"] = days_left > MIN_EX_DATE_DAYS

    # Dividend yield rule applies only to dividends.
    if category == "Dividend":
        if pd.isna(yield_value):
            checks["Dividend yield"] = None
        else:
            checks["Dividend yield"] = (
                yield_value > MIN_DIVIDEND_YIELD
            )

    failed = [
        name for name, result in checks.items()
        if result is False
    ]

    missing = [
        name for name, result in checks.items()
        if result is None
    ]

    # Missing required data means the decision is inconclusive.
    if missing:
        status = "NEEDS REVIEW"
        reason = "Missing data: " + ", ".join(missing)
    elif failed:
        status = "SELL"
        reason = "Failed: " + ", ".join(failed)
    else:
        status = "BUY"
        reason = "All applicable rules passed"

    return pd.Series({
        "Screening Status": status,
        "Screening Reason": reason,
    })


def apply_screening(data):
    data = data.copy()

    if data.empty:
        data["Screening Status"] = pd.Series(dtype=str)
        data["Screening Reason"] = pd.Series(dtype=str)
        return data

    results = data.apply(evaluate_security, axis=1)

    return pd.concat(
        [data.reset_index(drop=True),
         results.reset_index(drop=True)],
        axis=1,
    )


# =========================================================
# SCREENER PAGE
# =========================================================

def render_screener(data, security_code):
    matches = data[
        data["Security Code"].astype(str) == str(security_code)
    ].copy()

    if matches.empty:
        st.error(
            "This security is not present in the current date range. "
            "Return to the dashboard and try another security."
        )

        if st.button("← Back to dashboard"):
            st.query_params.clear()
            st.rerun()

        return

    if st.button("← Back to dashboard"):
        st.query_params.clear()
        st.rerun()

    row = matches.iloc[0]

    st.title("🔎 Security Screener")
    st.subheader(row["Security Name"])
    st.caption(
        f"Security Code: {security_code} · {row['Category']}"
    )

    price = row["Current Price"]
    yield_value = row["Dividend Yield (%)"]
    ex_date = row["Ex Date"]

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Current Price",
        f"₹{price:,.2f}" if pd.notna(price) else "Unavailable",
    )

    c2.metric(
        "Dividend Yield",
        (
            f"{yield_value:.2f}%"
            if row["Category"] == "Dividend"
            and pd.notna(yield_value)
            else "N/A"
        ),
    )

    c3.metric("Corporate Action", row["Category"])

    c4.metric(
        "Ex-Date",
        (
            ex_date.strftime("%d %b %Y")
            if pd.notna(ex_date)
            else "Unavailable"
        ),
    )

    st.markdown("---")
    st.subheader("Corporate Action Details")
    st.write(f"**Purpose:** {row['Purpose']}")

    st.markdown("---")
    st.subheader("Rule-by-Rule Results")

    checks = []

    if pd.isna(price) or price <= 0:
        checks.append((
            "Current price",
            "NEEDS REVIEW",
            "Price is missing or invalid.",
        ))
    elif price > MIN_CURRENT_PRICE:
        checks.append((
            "Current price",
            "PASS",
            f"₹{price:,.2f} is above ₹{MIN_CURRENT_PRICE:.0f}.",
        ))
    else:
        checks.append((
            "Current price",
            "FAIL",
            f"Price must be above ₹{MIN_CURRENT_PRICE:.0f}.",
        ))

    if pd.isna(ex_date):
        checks.append((
            "Ex-date",
            "NEEDS REVIEW",
            "Ex-date is unavailable.",
        ))
    else:
        days_left = (ex_date.date() - date.today()).days

        if days_left > MIN_EX_DATE_DAYS:
            checks.append((
                "Ex-date",
                "PASS",
                f"Ex-date is {days_left} days away.",
            ))
        else:
            checks.append((
                "Ex-date",
                "FAIL",
                f"Ex-date must be more than {MIN_EX_DATE_DAYS} days away.",
            ))

    if row["Category"] == "Dividend":
        if pd.isna(yield_value):
            checks.append((
                "Dividend yield",
                "NEEDS REVIEW",
                "Dividend yield could not be calculated.",
            ))
        elif yield_value > MIN_DIVIDEND_YIELD:
            checks.append((
                "Dividend yield",
                "PASS",
                f"{yield_value:.2f}% is above "
                f"{MIN_DIVIDEND_YIELD:.1f}%.",
            ))
        else:
            checks.append((
                "Dividend yield",
                "FAIL",
                f"Yield must be above {MIN_DIVIDEND_YIELD:.1f}%.",
            ))

    for rule_name, result, explanation in checks:
        with st.container(border=True):
            left, right = st.columns([1, 3])
            left.write(f"**{rule_name}**")
            right.write(f"**{result}** — {explanation}")

    st.markdown("---")
    st.subheader("Overall Screening Decision")

    status = row["Screening Status"]

    if status == "BUY":
        st.success("BUY — all applicable rules passed.")
    elif status == "SELL":
        st.error("SELL — one or more applicable rules failed.")
    else:
        st.warning(
            "NEEDS REVIEW — required information is missing."
        )

    st.caption(f"Reason: {row['Screening Reason']}")

    st.markdown("---")
    st.subheader("Other Corporate Actions for This Security")

    st.dataframe(
        matches[
            [
                "Category",
                "Purpose",
                "Ex Date",
                "Current Price",
                "Dividend Yield (%)",
                "Screening Status",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Ex Date": st.column_config.DateColumn(
                "Ex Date",
                format="DD-MMM-YYYY",
            ),
            "Current Price": st.column_config.NumberColumn(
                "Current Price (₹)",
                format="₹%.2f",
            ),
            "Dividend Yield (%)": st.column_config.NumberColumn(
                "Dividend Yield (%)",
                format="%.2f%%",
            ),
        },
    )

    st.info(
        "AI analysis will be added to this page in Phase 3. "
        "The current decision is based only on your defined rules."
    )

    st.caption(
        "Rule-based screening is not a guarantee of investment "
        "performance or personalized financial advice."
    )


# =========================================================
# TABLE RENDERING
# =========================================================

def render_table(data, category, table_key):
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

    part = part[columns].reset_index(drop=True)

    st.caption(
        "Select any row to open that security's individual screener."
    )

    event = st.dataframe(
        part,
        use_container_width=True,
        hide_index=True,
        height=min(560, 40 + 35 * len(part)),
        on_select="rerun",
        selection_mode="single-row",
        key=table_key,
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

    selected_rows = event.selection.rows

    if selected_rows:
        selected_index = selected_rows[0]

        if 0 <= selected_index < len(part):
            selected_code = str(
                part.iloc[selected_index]["Security Code"]
            )

            if selected_code.strip():
                st.query_params["security_code"] = selected_code
                st.rerun()


# =========================================================
# MAIN DASHBOARD
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
        "Quotes may be delayed or unavailable. Verify data before "
        "making investment decisions."
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
# LOAD DATA
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

except Exception as exc:
    st.error(
        "Could not fetch BSE data. The source may be unavailable, "
        "may have changed, or may be limiting requests."
    )

    st.markdown(f"[Check the BSE source page]({SOURCE_URL})")

    with st.expander("Technical details"):
        st.code(f"{type(exc).__name__}: {exc}")

    st.stop()


if actions.empty:
    st.warning(
        "No matching stock split, bonus or dividend records "
        "were returned for this ex-date range."
    )
    st.stop()


# =========================================================
# ENRICH DATA AND APPLY RULES
# =========================================================

with st.spinner("Fetching prices and applying screening rules…"):
    data = enrich(actions, history)
    data = apply_screening(data)


# =========================================================
# ROUTE TO SCREENER WHEN A ROW IS SELECTED
# =========================================================

selected_code = st.query_params.get("security_code")

if selected_code:
    render_screener(data, selected_code)
    st.stop()


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
    "BUY means all configured rules passed. It does not mean "
    "the security is guaranteed to be a good investment."
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
# TOP PICKS
# =========================================================

st.markdown("---")
st.subheader("🏆 Top Picks — BUY Candidates")

st.caption(
    "Select a row to open the individual security screener."
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
    ).reset_index(drop=True)

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

    event = st.dataframe(
        top_display,
        use_container_width=True,
        hide_index=True,
        height=min(300, 40 + 35 * len(top_display)),
        on_select="rerun",
        selection_mode="single-row",
        key="top_picks_table",
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

    selected_rows = event.selection.rows

    if selected_rows:
        selected_index = selected_rows[0]

        if 0 <= selected_index < len(top_display):
            selected_code = str(
                top_display.iloc[selected_index]["Security Code"]
            )

            if selected_code.strip():
                st.query_params["security_code"] = selected_code
                st.rerun()


# =========================================================
# CORPORATE ACTION TABLES
# =========================================================

st.markdown("---")

tab_split, tab_bonus, tab_dividend = st.tabs(
    ["Stock Split", "Bonus", "Dividend"]
)

with tab_split:
    render_table(data, "Stock Split", "stock_split_table")

with tab_bonus:
    render_table(data, "Bonus", "bonus_table")

with tab_dividend:
    render_table(data, "Dividend", "dividend_table")


# =========================================================
# CSV DOWNLOAD
# =========================================================

download = data.drop(
    columns=["_Dividend Amount", "_Category Order", "_Yield Sort"],
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
    "Dividend yield is estimated from identifiable dividend "
    "amounts in the previous 365 days divided by the current quote. "
    "Missing or unparseable data produces NEEDS REVIEW."
)

st.caption(
    "BSE data and quotes may be incomplete or delayed. Verify "
    "corporate actions, prices and dividend information before "
    "making investment decisions."
)
