# app_en.py
import streamlit as st
import pandas as pd
import requests
from io import StringIO
import altair as alt

st.set_page_config(page_title="VIF Elo (ClubElo) – Evolution", layout="wide")

st.title("VIF — Elo Evolution (ClubElo)")
st.caption("Source: api.clubelo.com • Step curve (Elo stays constant between matches)")

@st.cache_data(show_spinner=False)
def fetch_club_history(club_slug: str) -> pd.DataFrame:
    """Fetch full Elo history for a club from ClubElo CSV API and return step-ready series."""
    url = f"http://api.clubelo.com/{club_slug}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    # Parse dates and numeric Elo
    for c in ["From", "To"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    if "Elo" in df.columns:
        df["Elo"] = pd.to_numeric(df["Elo"], errors="coerce")
    df = df.dropna(subset=["Elo"]).sort_values("From").reset_index(drop=True)
    # Build step points: (From, Elo) and (To, Elo)
    rows = []
    for _, row in df.iterrows():
        f, t, elo = row["From"], row["To"], row["Elo"]
        if pd.isna(f):
            continue
        rows.append({"Date": f, "Elo": elo})
        if not pd.isna(t):
            rows.append({"Date": t, "Elo": elo})
    out = pd.DataFrame(rows).dropna().sort_values("Date")
    out["Club"] = club_slug
    return out

# --- Sidebar controls ---
st.sidebar.header("Controls")
default_team = "Valerenga"  # Vålerenga slug on ClubElo
team = st.sidebar.text_input("Main club (ClubElo slug)", value=default_team)

compare_raw = st.sidebar.text_input(
    "Compare (up to 3 clubs, comma-separated)",
    value=""
)

date_min = st.sidebar.date_input("Start date", value=None)
date_max = st.sidebar.date_input("End date", value=None)

rolling = st.sidebar.number_input("Moving average (matches)", min_value=0, max_value=50, value=0,
                                  help="0 = no smoothing. Applies a simple moving average per club.")

index_mode = st.sidebar.checkbox("Show change from first date (Δ Elo)", value=True,
                                 help="When enabled, plots Elo change relative to the first point in the selected period for each club. This makes small variations easier to see.")

st.sidebar.caption("Tip: use slugs as they appear in ClubElo URLs, e.g., Valerenga, Rosenborg, Molde, Brann.")

# --- Load data ---
all_series = []
error_msgs = []

def load_one(slug):
    try:
        s = fetch_club_history(slug.strip())
        return s
    except Exception as e:
        error_msgs.append(f"Could not fetch **{slug}**: {e}")
        return None

main_df = load_one(team)
if main_df is not None:
    all_series.append(main_df)

# comparison clubs
comp_list = [c.strip() for c in compare_raw.split(",") if c.strip()]
comp_list = comp_list[:3]
for cslug in comp_list:
    cdf = load_one(cslug)
    if cdf is not None:
        all_series.append(cdf)

if error_msgs:
    st.warning(" • ".join(error_msgs))

if not all_series:
    st.stop()

df = pd.concat(all_series, ignore_index=True)

# Date filter
if date_min:
    df = df[df["Date"] >= pd.to_datetime(date_min)]
if date_max:
    df = df[df["Date"] <= pd.to_datetime(date_max) + pd.to_timedelta(1, unit="D")]

# Smoothing
if rolling and rolling > 0:
    df = df.sort_values(["Club", "Date"])
    df["Elo_smoothed"] = df.groupby("Club")["Elo"].transform(lambda s: s.rolling(rolling, min_periods=1).mean())
    value_field = "Elo_smoothed"
else:
    value_field = "Elo"

# Index/Delta mode to improve scale visibility
if index_mode:
    df = df.sort_values(["Club", "Date"])
    first_vals = df.groupby("Club")[value_field].transform("first")
    df["Delta"] = df[value_field] - first_vals
    plot_field = "Delta"
    y_title = "Δ Elo (vs first date)"
else:
    plot_field = value_field
    y_title = "Elo"

# --- Chart ---
base = alt.Chart(df).mark_line(interpolate="step-after").encode(
    x=alt.X("Date:T", title="Date"),
    y=alt.Y(f"{plot_field}:Q", title=y_title),
    color=alt.Color("Club:N", title="Club"),
    tooltip=[
        alt.Tooltip("Club:N", title="Club"),
        alt.Tooltip("Date:T", title="Date"),
        alt.Tooltip("Elo:Q", title="Elo (raw)", format=".0f"),
        alt.Tooltip(f"{value_field}:Q", title="Elo (plotted basis)", format=".0f"),
        alt.Tooltip(f"{plot_field}:Q", title="Value shown", format=".0f"),
    ],
).properties(height=460)

st.altair_chart(base.interactive(), use_container_width=True)

# --- Quick metrics (main club) ---
st.subheader("Summary (main club)")
m = df[df["Club"] == team].copy()
if not m.empty:
    current = m.sort_values("Date").iloc[-1][value_field]
    start = m.sort_values("Date").iloc[0][value_field]
    delta = current - start
    col1, col2, col3 = st.columns(3)
    col1.metric("Latest Elo (in range)", f"{current:.0f}")
    col2.metric("First Elo (in range)", f"{start:.0f}")
    col3.metric("Change", f"{delta:+.0f}")
else:
    st.info("No data for the selected range.")

# --- Download data ---
st.subheader("Export")
export_cols = ["Date", "Club", "Elo"]
if "Elo_smoothed" in df.columns:
    export_cols.append("Elo_smoothed")
if "Delta" in df.columns:
    export_cols.append("Delta")

csv = df[export_cols].sort_values(["Club", "Date"]).to_csv(index=False)
st.download_button("Download CSV (current view)", data=csv, file_name="vif_elo_series.csv", mime="text/csv")

st.caption("""Notes:
- The ClubElo API returns intervals [From, To] where Elo is constant; the chart uses equivalent steps.
- Enable “Δ Elo” to better visualize changes (each club is rebased to zero at the first date in the selected window).
- Use the mouse wheel or drag to zoom/pan the chart.""")
