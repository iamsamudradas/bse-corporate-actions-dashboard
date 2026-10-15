from datetime import date, datetime, timedelta
from pathlib import Path
import re
import time

import pandas as pd
import streamlit as st

SOURCE_URL = "https://beta.bseindia.com/corporates/corporates_act.html"

st.set_page_config(
    page_title="BSE Corporate Actions",
    page_icon="📈",
    layout="wide",
)


def parse_date(value):
    if value is None or pd.isna(value) or not str(value).strip():
        return pd.NaT
    return pd.to_datetime(value, errors="coerce", dayfirst=True)


def get_value(row, *keys, default=None):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


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
    # Parse explicit rupee-per-share amounts only.
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
    rows = []

    # Support either a list of dictionaries or a DataFrame.
    if isinstance(records, pd.DataFrame):
        records = records.to_dict(orient="records")
    elif records is None:
        records = []
    elif isinstance(records, dict):
        records = [records]

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

        if not category:
            continue

        code = get_value(
            r,
            "scrip_code",
            "SCRIP_CD",
            "Security Code",
            "security_code",
            "scripcode",
        )

        rows.append({
            "Category": category,
            "Security Code": (
                str(code).strip() if code is not None else ""
            ),
            "Security Name": str(
                get_value(
                    r,
                    "long_name",
                    "company_name",
                    "Security Name",
                    "security_name",
                    "short_name",
                    default="",
                ) or ""
            ),
            "Ex Date": parse_date(
                get_value(
                    r,
                    "Ex_date",
                    "exdate",
                    "EX_DATE",
                    "Ex Date",
                    "ex_date",
                )
            ),
            "Purpose": purpose,
            "Record Date": parse_date(
                get_value(
                    r,
                    "RD_Date",
                    "record_date",
                    "Record Date",
                    "recordDate",
                )
            ),
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
        "Record Date",
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

    for key in (
        "LTP",
        "ltp",
        "LastPrice",
        "last_price",
        "CurrentPrice",
    ):
        try:
            if quote.get(key) not in (None, ""):
                return float(quote[key])
        except (AttributeError, TypeError, ValueError):
            pass

    return None


def enrich(actions, history, quote_limit=150):
    if actions.empty:
        actions = actions.copy()
        actions["Current Price"] = pd.Series(dtype=float)
        actions["Dividend Yield (%)"] = pd.Series(dtype=float)
        return actions

    codes = [
        x
        for x in actions["Security Code"].dropna().astype(str).unique()
        if x
    ]

    prices = {}

    # Limit quote requests to reduce load on the source.
    for i, code in enumerate(codes[:quote_limit]):
        try:
            prices[code] = fetch_price(code)
        except Exception:
            prices[code] = None

        if i < min(len(codes), quote_limit) - 1:
            time.sleep(0.08)

    out = actions.copy()
    out["Current Price"] = out["Security Code"].map(prices)

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
        total = totals.get(str(row["Security Code"]))

        if (
            pd.isna(price)
            or not price
            or price <= 0
            or total is None
        ):
            return None

        return round(float(total) / float(price) * 100, 2)

    out["Dividend Yield (%)"] = out.apply(
        calculate_yield,
        axis=1,
    )

    return out


def render_table(df, category):
    st.subheader(category)

    if df.empty:
        part = pd.DataFrame()
    else:
        part = df[df["Category"] == category].copy()

    if part.empty:
        st.info(
            f"No {category.lower()} records found for this date range."
        )
        return

    part["Ex Date"] = pd.to_datetime(
        part["Ex Date"],
        errors="coerce",
    )

    part = part.rename(
        columns={"Current Price": "Current Price (₹)"}
    )

    # Required column order.
    if category == "Dividend":
        columns = [
            "Security Code",
            "Security Name",
            "Current Price (₹)",
            "Dividend Yield (%)",
            "Purpose",
            "Ex Date",
        ]
    else:
        columns = [
            "Security Code",
            "Security Name",
            "Current Price (₹)",
            "Purpose",
            "Ex Date",
        ]

    # Record Date is deliberately excluded.
    part = part[columns]

    # Keep the actual date type for sorting, and format for display.
    part["Ex Date"] = part["Ex Date"].dt.strftime(
        "%d-%b-%Y"
    ).fillna("—")

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
                width="large",
            ),
            "Purpose": st.column_config.TextColumn(
                "Purpose",
                width="large",
            ),
            "Current Price (₹)": st.column_config.NumberColumn(
                "Current Price (₹)",
                format="₹%.2f",
            ),
            "Dividend Yield (%)": st.column_config.NumberColumn(
                "Dividend Yield (%)",
                format="%.2f%%",
            ),
        },
    )

    st.caption(
        "Click a column header to sort the table."
    )


# ---------------- PAGE HEADER ----------------

st.title("📈 BSE Corporate Actions Dashboard")

st.caption(
    "Stock splits · Bonus issues · Dividends · BSE quote data"
)


# ---------------- SIDEBAR CONTROLS ----------------

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

    st.markdown(
        f"[Open BSE source page]({SOURCE_URL})"
    )

    st.caption(
        "Quotes are best-effort and may be delayed; "
        "they are not guaranteed real-time."
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


# ---------------- FETCH DATA ----------------

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
        "Could not fetch BSE data. The exchange endpoint may "
        "be unavailable, changed, or limiting requests."
    )

    st.markdown(
        f"Check the [BSE corporate actions page]({SOURCE_URL}) "
        "and try again later."
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


# ---------------- ENRICH DATA ----------------

with st.spinner(
    "Fetching quotes and calculating dividend yields…"
):
    data = enrich(actions, history)


# ---------------- SUMMARY METRICS ----------------

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "Stock splits",
    int((data["Category"] == "Stock Split").sum()),
)

c2.metric(
    "Bonus issues",
    int((data["Category"] == "Bonus").sum()),
)

c3.metric(
    "Dividends",
    int((data["Category"] == "Dividend").sum()),
)

c4.metric(
    "Quotes available",
    f"{int(data['Current Price'].notna().sum())}/{len(data)}",
)

st.caption(
    f"Last fetched: "
    f"{datetime.now().astimezone().strftime('%d %b %Y, %I:%M:%S %p %Z')} "
    "· Data is cached for five minutes."
)


# ---------------- SEARCH ----------------

query = st.text_input(
    "Search all tables",
    placeholder="Security code, company name or purpose",
)

if query.strip():
    mask = (
        data["Security Code"].astype(str).str.contains(
            query,
            case=False,
            na=False,
        )
        | data["Security Name"].str.contains(
            query,
            case=False,
            na=False,
        )
        | data["Purpose"].str.contains(
            query,
            case=False,
            na=False,
        )
    )

    data = data[mask]


# ---------------- TABLES ----------------

tab_split, tab_bonus, tab_dividend = st.tabs(
    ["Stock Split", "Bonus", "Dividend"]
)

with tab_split:
    render_table(data, "Stock Split")

with tab_bonus:
    render_table(data, "Bonus")

with tab_dividend:
    render_table(data, "Dividend")


# ---------------- CSV DOWNLOAD ----------------

download = data.drop(
    columns=[
        "_Dividend Amount",
        "Record Date",
    ],
    errors="ignore",
).copy()

if "Ex Date" in download.columns:
    download["Ex Date"] = pd.to_datetime(
        download["Ex Date"],
        errors="coerce",
    ).dt.strftime("%Y-%m-%d")

st.download_button(
    "⬇ Download filtered data (CSV)",
    download.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"bse_corporate_actions_{today.isoformat()}.csv",
    mime="text/csv",
)


# ---------------- FOOTER ----------------

st.markdown("---")

st.caption(
    "Dividend yield is calculated using identifiable "
    "rupee-per-share dividends in the previous 365 days "
    "divided by the current quote, multiplied by 100. "
    "Missing or unparseable data results in a blank yield."
)

st.caption(
    "Verify corporate actions and prices with BSE before "
    "making investment decisions."
)