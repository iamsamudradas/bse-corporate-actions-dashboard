# BSE Corporate Actions Dashboard

Streamlit dashboard with separate sortable tables:
- **Stock Split:** Security Code, Security Name, Ex Date, Purpose, Record Date, Current Price
- **Bonus:** same six columns
- **Dividend:** same columns plus Dividend Yield (%)

## Run on Windows
1. Install Python 3.11+.
2. Extract this ZIP and open PowerShell in the extracted folder.
3. Run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

Open the local URL Streamlit prints, usually `http://localhost:8501`.

## Features
- Select ex-date range.
- Click **Refresh BSE data** to clear cached data and refetch.
- Click column headers to sort.
- Search by security code, company name or purpose.
- Download filtered results as CSV.

## Data-source caveats
The app uses the community-maintained `bse` Python wrapper (not an official BSE SDK) to retrieve corporate actions and BSE quotes. BSE may change endpoints or limit requests. The quote is best-effort and may be delayed; it is not guaranteed real-time. For guaranteed real-time data or production reliability, use an authorised feed from https://marketdata.bseindia.com/.

Dividend yield is calculated as the sum of explicitly parseable rupee-per-share dividends found in the prior 365 days divided by the current quote. If the amount, history or price is unavailable, the yield is left blank rather than guessed. Dividend purposes stated as a percentage of face value are not converted automatically.

The first version limits price lookups to 150 unique securities per refresh to avoid sending too many requests. Quotes and action data are cached for five minutes. Verify corporate actions and prices with BSE before making investment decisions.
