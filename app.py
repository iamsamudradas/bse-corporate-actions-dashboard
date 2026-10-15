from datetime import date, datetime, timedelta
from pathlib import Path
import re
import time

import pandas as pd
import streamlit as st

SOURCE_URL = "https://beta.bseindia.com/corporates/corporates_act.html"
MIN_DIVIDEND_YIELD = 3.0
MIN_EX_DATE_DAYS = 7
MIN_CURRENT_PRICE = 100.0
QUOTE_LIMIT = 150

st.set_page_config(page_title="Corporate Actions Intelligence", page_icon="📈", layout="wide")

# Dark financial-terminal styling
st.markdown("""
<style>
:root { --bg:#0b1220; --panel:#111c2e; --border:#26364d; --text:#edf3fb;
        --muted:#9fb0c6; --green:#45d6a0; --red:#ff8585; --amber:#f0c96d; }
.stApp { background:#0b1220; color:#edf3fb; }
[data-testid="stHeader"] { background:rgba(11,18,32,.96); }
[data-testid="stSidebar"] { background:#0e192a; border-right:1px solid #26364d; }
[data-testid="stSidebar"] * { color:#edf3fb; }
h1,h2,h3,h4,p,label,[data-testid="stMarkdownContainer"] { color:#edf3fb; }
.hero { background:linear-gradient(120deg,#14243a,#101a2b 70%,#132b35);
        border:1px solid #2a3c55; border-radius:18px; padding:25px 28px; margin:2px 0 20px; }
.eyebrow,.section-kicker { color:#9db5d3; font-size:.72rem; font-weight:700;
        letter-spacing:.14em; text-transform:uppercase; margin-bottom:8px; }
.hero-title { color:#f4f8ff; font-size:2rem; font-weight:750; line-height:1.15; margin-bottom:10px; }
.hero-copy { color:#b8c7da; font-size:.98rem; line-height:1.6; margin:0; }
.purpose-card { background:#101b2c; border:1px solid #26364d; border-radius:14px;
        padding:17px 19px; margin:0 0 18px; }
.purpose-card p { color:#b9c8dc; line-height:1.6; margin-bottom:0; }
.status-pill { display:inline-block; padding:5px 10px; border-radius:999px;
        font-weight:700; font-size:.76rem; }
.status-buy { background:rgba(69,214,160,.13); color:#63e4b0; border:1px solid rgba(69,214,160,.32); }
.status-sell { background:rgba(255,133,133,.13); color:#ff9a9a; border:1px solid rgba(255,133,133,.3); }
.status-review { background:rgba(240,201,109,.13); color:#f3d27d; border:1px solid rgba(240,201,109,.3); }
[data-testid="stMetric"] { background:#111c2e; border:1px solid #26364d; padding:16px 18px; border-radius:14px; }
[data-testid="stMetricLabel"] { color:#aabbd0 !important; }
[data-testid="stMetricValue"] { color:#f4f8ff !important; font-weight:750; }
[data-testid="stDataFrame"] { border:1px solid #26364d; border-radius:12px; overflow:hidden; }
.stTabs [data-baseweb="tab-list"] { gap:8px; border-bottom:1px solid #26364d; }
.stTabs [data-baseweb="tab"] { background:#111c2e; border:1px solid #26364d;
        border-radius:9px 9px 0 0; padding:9px 16px; }
.stTabs [aria-selected="true"] { background:#20344e !important; border-bottom-color:#8db8ff !important; }
.stButton > button,.stDownloadButton > button { border-radius:9px; border:1px solid #38506d;
        background:#1b2c43; color:#f4f8ff; font-weight:650; }
.stButton > button:hover,.stDownloadButton > button:hover { border-color:#8db8ff; color:white; background:#263d5a; }
div[data-baseweb="input"] > div,div[data-baseweb="select"] > div,[data-testid="stDateInput"] input {
        background:#101b2c; border-color:#31445e; color:#edf3fb; }
.small-note { color:#93a6bf; font-size:.82rem; line-height:1.5; }
hr { border-color:#26364d; }
@media(max-width:700px){ .hero{padding:19px}.hero-title{font-size:1.55rem} }
</style>
""", unsafe_allow_html=True)


# ------------------------ DATA HELPERS ------------------------

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
    if any(term in p for term in (
        "stock split", "split from", "split of",
        "sub-division", "sub division", "subdivision"
    )):
        return "Stock Split"
    return None


def dividend_amount(purpose):
    p = str(purpose or "").lower().replace(",", "")
    for pattern in (
        r"rs\.?\s*[-:]?\s*(\d+(?:\.\d+)?)",
        r"₹\s*(\d+(?:\.\d+)?)",
        r"rupees?\s*(\d+(?:\.\d+)?)",
    ):
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
    for r in records:
        if not isinstance(r, dict):
            continue
        purpose = str(get_value(r, "Purpose", "purpose", "purpose_desc", default="") or "").strip()
        category = classify(purpose)
        if category is None:
            continue
        code = get_value(r, "scrip_code", "SCRIP_CD", "Security Code", "security_code", "scripcode")
        name = get_value(r, "long_name", "company_name", "Security Name", "security_name", "short_name", default="")
        ex_date = parse_date(get_value(r, "Ex_date", "exdate", "EX_DATE", "Ex Date", "ex_date"))
        rows.append({
            "Category": category,
            "Security Code": str(code).strip() if code is not None else "",
            "Security Name": str(name or ""),
            "Ex Date": ex_date,
            "Purpose": purpose,
            "_Dividend Amount": dividend_amount(purpose) if category == "Dividend" else None,
        })

    cols = ["Category", "Security Code", "Security Name", "Ex Date", "Purpose", "_Dividend Amount"]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df = df.drop_duplicates(subset=["Category", "Security Code", "Ex Date", "Purpose"])
        df = df.sort_values(["Ex Date", "Security Name"], na_position="last")
    return df.reset_index(drop=True)


# ------------------------ BSE FETCHING ------------------------

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
    for key in ("LTP", "ltp", "LastPrice", "last_price", "CurrentPrice"):
        try:
            value = quote.get(key)
            if value not in (None, "") and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def enrich(actions, history):
    out = actions.copy()
    if out.empty:
        out["Current Price"] = pd.Series(dtype=float)
        out["Dividend Yield (%)"] = pd.Series(dtype=float)
        return out

    codes = [x for x in out["Security Code"].dropna().astype(str).unique() if x.strip()]
    prices = {}
    for i, code in enumerate(codes[:QUOTE_LIMIT]):
        try:
            prices[code] = fetch_price(code)
        except Exception:
            prices[code] = None
        if i < min(len(codes), QUOTE_LIMIT) - 1:
            time.sleep(0.08)
    out["Current Price"] = out["Security Code"].map(prices)

    totals = {}
    if not history.empty:
        h = history[history["Category"] == "Dividend"].copy()
        h["_Dividend Amount"] = pd.to_numeric(h["_Dividend Amount"], errors="coerce")
        h = h.dropna(subset=["_Dividend Amount"])
        totals = h.groupby("Security Code")["_Dividend Amount"].sum().to_dict()

    def calc_yield(row):
        if row["Category"] != "Dividend":
            return None
        price = row["Current Price"]
        amount = totals.get(str(row["Security Code"]))
        if pd.isna(price) or price <= 0 or amount is None:
            return None
        return round(float(amount) / float(price) * 100, 2)

    out["Dividend Yield (%)"] = out.apply(calc_yield, axis=1)
    return out


# ------------------------ SCREENING RULES ------------------------

def evaluate_security(row):
    checks = {}
    price, ex_date = row["Current Price"], row["Ex Date"]
    yield_value = row["Dividend Yield (%)"]

    if pd.isna(price) or price <= 0:
        checks["Current price"] = None
    else:
        checks["Current price"] = price > MIN_CURRENT_PRICE

    if pd.isna(ex_date):
        checks["Ex-date"] = None
    else:
        days_left = (pd.Timestamp(ex_date).date() - date.today()).days
        checks["Ex-date"] = days_left > MIN_EX_DATE_DAYS

    if row["Category"] == "Dividend":
        checks["Dividend yield"] = (
            None if pd.isna(yield_value)
            else yield_value > MIN_DIVIDEND_YIELD
        )

    failed = [k for k, v in checks.items() if v is False]
    missing = [k for k, v in checks.items() if v is None]
    if missing:
        status, reason = "NEEDS REVIEW", "Missing data: " + ", ".join(missing)
    elif failed:
        status, reason = "SELL", "Failed: " + ", ".join(failed)
    else:
        status, reason = "BUY", "All applicable rules passed"

    return pd.Series({"Screening Status": status, "Screening Reason": reason})


def apply_screening(data):
    data = data.copy()
    if data.empty:
        data["Screening Status"] = pd.Series(dtype=str)
        data["Screening Reason"] = pd.Series(dtype=str)
        return data
    results = data.apply(evaluate_security, axis=1)
    return pd.concat([data.reset_index(drop=True), results.reset_index(drop=True)], axis=1)


# ------------------------ UI HELPERS ------------------------

def status_badge(status):
    cls = {"BUY": "status-buy", "SELL": "status-sell"}.get(status, "status-review")
    return f'<span class="status-pill {cls}">{status}</span>'


def render_screener(data, security_code):
    matches = data[data["Security Code"].astype(str) == str(security_code)].copy()
    if matches.empty:
        st.error("This security is not present in the current date range.")
        if st.button("← Back to dashboard"):
            st.query_params.clear()
            st.rerun()
        return

    if st.button("← Back to dashboard"):
        st.query_params.clear()
        st.rerun()

    row = matches.iloc[0]
    st.markdown("""
    <div class="hero">
      <div class="eyebrow">Security research profile</div>
      <div class="hero-title">Individual Security Screener</div>
      <p class="hero-copy">Review the corporate action, inspect each rule,
      and understand why this security received its screening status.</p>
    </div>
    """, unsafe_allow_html=True)

    st.subheader(str(row["Security Name"] or "Security"))
    st.caption(f"Security code: {security_code} · {row['Category']}")

    price, yield_value, ex_date = row["Current Price"], row["Dividend Yield (%)"], row["Ex Date"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current Price", f"₹{price:,.2f}" if pd.notna(price) else "Unavailable")
    c2.metric("Dividend Yield", f"{yield_value:.2f}%" if row["Category"] == "Dividend" and pd.notna(yield_value) else "N/A")
    c3.metric("Corporate Action", row["Category"])
    c4.metric("Ex-Date", ex_date.strftime("%d %b %Y") if pd.notna(ex_date) else "Unavailable")

    st.markdown("### Corporate action details")
    st.write(f"**Purpose:** {row['Purpose']}")
    st.markdown("### Rule-by-rule assessment")

    checks = []
    if pd.isna(price) or price <= 0:
        checks.append(("Current price", "NEEDS REVIEW", "Price is missing or invalid."))
    elif price > MIN_CURRENT_PRICE:
        checks.append(("Current price", "PASS", f"₹{price:,.2f} is above ₹{MIN_CURRENT_PRICE:.0f}."))
    else:
        checks.append(("Current price", "FAIL", f"Price must be above ₹{MIN_CURRENT_PRICE:.0f}."))

    if pd.isna(ex_date):
        checks.append(("Ex-date", "NEEDS REVIEW", "Ex-date is unavailable."))
    else:
        days_left = (ex_date.date() - date.today()).days
        if days_left > MIN_EX_DATE_DAYS:
            checks.append(("Ex-date", "PASS", f"Ex-date is {days_left} days away."))
        else:
            checks.append(("Ex-date", "FAIL", f"Ex-date must be more than {MIN_EX_DATE_DAYS} days away."))

    if row["Category"] == "Dividend":
        if pd.isna(yield_value):
            checks.append(("Dividend yield", "NEEDS REVIEW", "Dividend yield could not be calculated."))
        elif yield_value > MIN_DIVIDEND_YIELD:
            checks.append(("Dividend yield", "PASS", f"{yield_value:.2f}% is above {MIN_DIVIDEND_YIELD:.1f}%."))
        else:
            checks.append(("Dividend yield", "FAIL", f"Yield must be above {MIN_DIVIDEND_YIELD:.1f}%."))

    for name, result, explanation in checks:
        with st.container(border=True):
            left, middle, right = st.columns([1.2, 1, 3])
            left.write(f"**{name}**")
            middle.markdown(status_badge(result), unsafe_allow_html=True)
            right.write(explanation)

    st.markdown("### Overall screening decision")
    st.markdown(status_badge(row["Screening Status"]), unsafe_allow_html=True)
    st.write(row["Screening Reason"])

    st.markdown("### Other corporate actions for this security")
    st.dataframe(
        matches[["Category", "Purpose", "Ex Date", "Current Price", "Dividend Yield (%)", "Screening Status"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Ex Date": st.column_config.DateColumn("Ex Date", format="DD-MMM-YYYY"),
            "Current Price": st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
            "Dividend Yield (%)": st.column_config.NumberColumn("Dividend Yield (%)", format="%.2f%%"),
        },
    )

    st.markdown("""
    <div class="purpose-card">
      <div class="section-kicker">Coming next · AI research assistant</div>
      <p>This profile can later include AI-generated explanations, risk flags,
      dividend sustainability checks and supporting announcements. For now,
      the result is determined only by the configured rules.</p>
    </div>
    """, unsafe_allow_html=True)
    st.caption("Rule-based screening is not a guarantee of investment performance or personalized financial advice.")


def render_table(data, category, key):
    st.subheader(category)
    part = data[data["Category"] == category].copy()
    if part.empty:
        st.info(f"No {category.lower()} records found for this date range.")
        return

    columns = (
        ["Security Code", "Security Name", "Current Price", "Dividend Yield (%)",
         "Purpose", "Ex Date", "Screening Status", "Screening Reason"]
        if category == "Dividend"
        else ["Security Code", "Security Name", "Current Price", "Purpose",
              "Ex Date", "Screening Status", "Screening Reason"]
    )
    part = part[columns].reset_index(drop=True)
    st.caption("Select a row to open its individual security screener.")

    event = st.dataframe(
        part, use_container_width=True, hide_index=True,
        height=min(560, 40 + 35 * len(part)),
        on_select="rerun", selection_mode="single-row", key=key,
        column_config={
            "Security Code": st.column_config.TextColumn("Security Code"),
            "Security Name": st.column_config.TextColumn("Security Name", width="medium"),
            "Current Price": st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
            "Dividend Yield (%)": st.column_config.NumberColumn("Dividend Yield (%)", format="%.2f%%"),
            "Purpose": st.column_config.TextColumn("Purpose", width="large"),
            "Ex Date": st.column_config.DateColumn("Ex Date", format="DD-MMM-YYYY"),
            "Screening Status": st.column_config.TextColumn("Screening Status"),
            "Screening Reason": st.column_config.TextColumn("Screening Reason", width="large"),
        },
    )
    if event.selection.rows:
        idx = event.selection.rows[0]
        if 0 <= idx < len(part):
            code = str(part.iloc[idx]["Security Code"])
            if code.strip():
                st.query_params["security_code"] = code
                st.rerun()


# ------------------------ HEADER AND PURPOSE ------------------------

selected_code = st.query_params.get("security_code")
if not selected_code:
    st.markdown("""
    <div class="hero">
      <div class="eyebrow">BSE equity · Corporate actions intelligence</div>
      <div class="hero-title">Corporate Actions Intelligence</div>
      <p class="hero-copy">A rule-based research dashboard for discovering
      upcoming BSE dividends, stock splits and bonus issues. It screens
      securities against transparent criteria, highlights candidates that
      meet those criteria, and shows which rules passed or failed—helping
      you focus your research more efficiently.</p>
    </div>
    """, unsafe_allow_html=True)

    with st.expander("What this website does and why it exists"):
        st.markdown("""
        **Purpose**

        This website consolidates BSE corporate-action information and applies
        transparent screening rules to help shortlist securities for further research.

        - **Track corporate actions:** review dividend announcements, stock splits
          and bonus issues with their ex-dates and purpose.
        - **Screen dividend candidates:** dividend yield must be above 3%,
          ex-date more than 7 days away, and price above ₹100.
        - **Screen stock splits and bonus issues:** ex-date must be more than
          7 days away and price must be above ₹100.
        - **Understand results:** BUY means all applicable rules pass; SELL means
          one or more rules fail; NEEDS REVIEW means required data is missing.
        - **Inspect a security:** select a table row to open its individual screener.
        - **Export data:** download filtered results as CSV.

        **Important limitation:** this is a first-pass rules screen, not a complete
        valuation model. It does not yet establish whether a dividend is sustainable,
        whether a share is fairly valued, or whether a corporate action makes an
        investment attractive. Verify prices and announcements with official sources.
        """)

with st.sidebar:
    st.markdown("## CONTROL ROOM")
    st.caption("Screening parameters")
    today = date.today()
    start = st.date_input("Ex-date from", today)
    end = st.date_input("Ex-date to", today + timedelta(days=365))
    st.markdown("---")
    st.markdown("**Active rules**")
    st.caption(f"Dividend yield > {MIN_DIVIDEND_YIELD:.1f}%")
    st.caption(f"Ex-date > {MIN_EX_DATE_DAYS} days away")
    st.caption(f"Current price > ₹{MIN_CURRENT_PRICE:.0f}")
    st.markdown("---")
    st.markdown(f"[Open BSE source page]({SOURCE_URL})")
    st.markdown('<p class="small-note">Quotes may be delayed or unavailable. Verify data before investing.</p>', unsafe_allow_html=True)
    if st.button("↻ Refresh BSE data", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if start > end:
    st.error("Start date must be on or before end date.")
    st.stop()


# ------------------------ LOAD DATA ------------------------

today = date.today()
try:
    with st.spinner("Fetching corporate actions from BSE…"):
        actions = normalise(fetch_actions(
            datetime.combine(start, datetime.min.time()).isoformat(),
            datetime.combine(end, datetime.max.time()).isoformat(),
        ))
        history = normalise(fetch_actions(
            datetime.combine(today - timedelta(days=365), datetime.min.time()).isoformat(),
            datetime.combine(today, datetime.max.time()).isoformat(),
        ))
except Exception as exc:
    st.error("Could not fetch BSE data. The source may be unavailable, changed, or limiting requests.")
    st.markdown(f"[Check the BSE source page]({SOURCE_URL})")
    with st.expander("Technical details"):
        st.code(f"{type(exc).__name__}: {exc}")
    st.stop()

if actions.empty:
    st.warning("No matching stock split, bonus or dividend records were returned for this ex-date range.")
    st.stop()

with st.spinner("Fetching prices and applying screening rules…"):
    data = apply_screening(enrich(actions, history))


# ------------------------ SCREENER ROUTE ------------------------

selected_code = st.query_params.get("security_code")
if selected_code:
    render_screener(data, selected_code)
    st.stop()


# ------------------------ SUMMARY ------------------------

buy_count = int((data["Screening Status"] == "BUY").sum())
sell_count = int((data["Screening Status"] == "SELL").sum())
review_count = int((data["Screening Status"] == "NEEDS REVIEW").sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("BUY candidates", buy_count)
c2.metric("SELL", sell_count)
c3.metric("Needs review", review_count)
c4.metric("Securities screened", len(data))
st.caption("BUY means all configured rules passed. It does not guarantee a security is a good investment.")


# ------------------------ SEARCH ------------------------

query = st.text_input("⌕ Search securities", placeholder="Search security code, company name or purpose…")
if query.strip():
    mask = (
        data["Security Code"].astype(str).str.contains(query, case=False, na=False)
        | data["Security Name"].astype(str).str.contains(query, case=False, na=False)
        | data["Purpose"].astype(str).str.contains(query, case=False, na=False)
    )
    data = data[mask].copy()


# ------------------------ TOP PICKS ------------------------

st.markdown("---")
st.markdown("### Top Picks")
st.caption("Rule-based candidates for further research · Select a row to inspect it.")
top = data[data["Screening Status"] == "BUY"].copy()

if top.empty:
    st.info("No securities currently pass all applicable rules.")
else:
    top["_Category Order"] = top["Category"].map({"Dividend": 0, "Stock Split": 1, "Bonus": 2}).fillna(3)
    top["_Yield Sort"] = top["Dividend Yield (%)"].fillna(-1)
    top = top.sort_values(
        ["_Category Order", "_Yield Sort", "Ex Date"],
        ascending=[True, False, True], na_position="last"
    ).reset_index(drop=True)
    top_display = top[
        ["Security Code", "Security Name", "Category", "Current Price",
         "Dividend Yield (%)", "Ex Date", "Purpose"]
    ].copy()

    event = st.dataframe(
        top_display, use_container_width=True, hide_index=True,
        height=min(300, 40 + 35 * len(top_display)),
        on_select="rerun", selection_mode="single-row", key="top_picks_table",
        column_config={
            "Security Code": st.column_config.TextColumn("Security Code"),
            "Security Name": st.column_config.TextColumn("Security Name", width="medium"),
            "Category": st.column_config.TextColumn("Corporate Action"),
            "Current Price": st.column_config.NumberColumn("Current Price (₹)", format="₹%.2f"),
            "Dividend Yield (%)": st.column_config.NumberColumn("Dividend Yield (%)", format="%.2f%%"),
            "Ex Date": st.column_config.DateColumn("Ex Date", format="DD-MMM-YYYY"),
            "Purpose": st.column_config.TextColumn("Purpose", width="large"),
        },
    )
    if event.selection.rows:
        idx = event.selection.rows[0]
        if 0 <= idx < len(top_display):
            code = str(top_display.iloc[idx]["Security Code"])
            if code.strip():
                st.query_params["security_code"] = code
                st.rerun()


# ------------------------ ACTION TABLES ------------------------

st.markdown("---")
tab_split, tab_bonus, tab_dividend = st.tabs(["Stock Split", "Bonus", "Dividend"])
with tab_split:
    render_table(data, "Stock Split", "stock_split_table")
with tab_bonus:
    render_table(data, "Bonus", "bonus_table")
with tab_dividend:
    render_table(data, "Dividend", "dividend_table")


# ------------------------ CSV EXPORT ------------------------

download = data.drop(columns=["_Dividend Amount", "_Category Order", "_Yield Sort"], errors="ignore").copy()
download["Ex Date"] = pd.to_datetime(download["Ex Date"], errors="coerce").dt.strftime("%Y-%m-%d")
st.markdown("---")
st.download_button(
    "⬇ Download filtered data (CSV)",
    download.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"bse_corporate_actions_{today.isoformat()}.csv",
    mime="text/csv",
)


# ------------------------ FOOTER ------------------------

st.markdown("""
<div class="purpose-card">
  <div class="section-kicker">About this screener</div>
  <p>Corporate Actions Intelligence helps shortlist upcoming BSE corporate
  actions using transparent, user-defined rules. It is a research aid, not an
  automated trading system or a guarantee of investment returns. Data availability,
  dividend parsing and quote freshness can affect results. Verify information
  with BSE and the relevant company before acting on it.</p>
</div>
""", unsafe_allow_html=True)
st.caption(
    "Dividend yield is estimated from identifiable dividend amounts in the previous 365 days divided by the current quote. "
    "Missing or unparseable data produces NEEDS REVIEW."
)
