# app_en_nt.py (English UI)
import streamlit as st
import pandas as pd
import requests
from io import StringIO
import altair as alt
import re
from datetime import datetime

st.set_page_config(page_title="VIF Elo – Clubs & National Teams", layout="wide")

st.title("VIF — Elo Evolution (Clubs & National Teams)")
st.caption("Sources: api.clubelo.com (clubs) • eloratings.net (national teams)")

# ---------------------------
# Utilities
# ---------------------------
UA = {"User-Agent": "Mozilla/5.0 (compatible; EloApp/1.0; +https://example.com)"}

def _safe_get(url: str, timeout=30):
    r = requests.get(url, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r

def _stepify(df: pd.DataFrame, date_col: str, value_col: str, label_col: str, label_val: str):
    """Generate step points: (Date, Elo) and (next Date, Elo)."""
    if df.empty:
        return df
    df = df.sort_values(date_col).reset_index(drop=True)
    rows = []
    for i, row in df.iterrows():
        d = row[date_col]
        v = row[value_col]
        rows.append({"Date": d, "Elo": v, "Entity": label_val})
        if i + 1 < len(df):
            d2 = df.loc[i + 1, date_col]
            rows.append({"Date": d2, "Elo": v, "Entity": label_val})
    out = pd.DataFrame(rows)
    return out

# ---------------------------
# ClubElo (clubs)
# ---------------------------
@st.cache_data(show_spinner=False)
def fetch_club_history(club_slug: str) -> pd.DataFrame:
    url = f"http://api.clubelo.com/{club_slug}"
    r = _safe_get(url)
    df = pd.read_csv(StringIO(r.text))
    # parse dates & Elo
    for c in ["From", "To"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    if "Elo" in df.columns:
        df["Elo"] = pd.to_numeric(df["Elo"], errors="coerce")
    df = df.dropna(subset=["Elo"]).sort_values("From").reset_index(drop=True)
    rows = []
    for _, row in df.iterrows():
        f, t, elo = row["From"], row["To"], row["Elo"]
        if pd.isna(f):
            continue
        rows.append({"Date": f, "Elo": elo})
        if not pd.isna(t):
            rows.append({"Date": t, "Elo": elo})
    out = pd.DataFrame(rows).dropna().sort_values("Date")
    out["Entity"] = club_slug
    return out

# ---------------------------
# EloRatings (national teams)
# ---------------------------
YEARS_MIN = 1901
YEARS_MAX = datetime.utcnow().year

def _parse_year_snapshot(text: str, team_name: str):
    """
    Annual page is plain text with lines like:
    '1. Spain. 2182'
    Returns numeric Elo for team_name, or None.
    """
    lines = text.splitlines()
    team_pat = re.compile(r"^\s*\d+\.\s*(.+?)\.\s*(\d+)\s*$", flags=re.IGNORECASE)
    for ln in lines:
        m = team_pat.match(ln.strip())
        if not m:
            continue
        name = m.group(1).strip()
        elo = m.group(2).strip()
        if name.lower() == team_name.lower():
            try:
                return int(elo)
            except:
                return None
    return None

@st.cache_data(show_spinner=False)
def fetch_national_history_yearly(team_name: str,
                                  year_start: int = YEARS_MIN,
                                  year_end: int = YEARS_MAX) -> pd.DataFrame:
    """
    Crawl yearly snapshots on eloratings.net and create an annual step series (31 Dec each year).
    """
    records = []
    for y in range(max(YEARS_MIN, year_start), min(YEARS_MAX, year_end) + 1):
        url = f"https://www.eloratings.net/{y}"
        try:
            r = _safe_get(url, timeout=20)
            txt = r.text
            elo_val = _parse_year_snapshot(txt, team_name)
            if elo_val is not None:
                d = pd.Timestamp(f"{y}-12-31")
                records.append({"Date": d, "Elo": elo_val})
        except Exception:
            continue

    if not records:
        raise ValueError(f"Could not find Elo for '{team_name}' in annual snapshots.")
    df = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)
    step = _stepify(df, "Date", "Elo", "Entity", team_name)
    return step

# (Optional) Try chart endpoints if any are publicly available

def _try_graph_endpoints(team_name: str) -> pd.DataFrame | None:
    """
    Some eloratings deployments expose chart data via helper endpoints.
    This function tries a few known patterns. If none work, returns None.
    """
    candidates = [
        f"https://www.eloratings.net/graph?team={team_name}",
        f"https://www.eloratings.net/graph?second={team_name}",
        f"https://www.eloratings.net/{team_name}_graph",
    ]
    for url in candidates:
        try:
            r = _safe_get(url, timeout=15)
            txt = r.text
            rows = []
            for ln in txt.splitlines():
                ln = ln.strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}\D+\d+(\.\d+)?$", ln):
                    parts = re.split(r"[;, \t]+", ln)
                    if len(parts) >= 2:
                        try:
                            d = pd.to_datetime(parts[0], errors="raise")
                            v = float(parts[1])
                            rows.append({"Date": d, "Elo": v})
                        except:
                            pass
            if rows:
                df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
                out = _stepify(df, "Date", "Elo", "Entity", team_name)
                return out
        except Exception:
            continue
    return None

@st.cache_data(show_spinner=False)
def fetch_selection_history(team_name: str,
                            prefer_graph_endpoint: bool = True,
                            year_start: int = YEARS_MIN,
                            year_end: int = YEARS_MAX) -> pd.DataFrame:
    """
    Retrieve Elo time series for a national team.
    1) Try chart endpoints (if available)
    2) Fallback: yearly snapshots (31 Dec) with step curve
    """
    if prefer_graph_endpoint:
        df = _try_graph_endpoints(team_name)
        if df is not None and not df.empty:
            return df
    return fetch_national_history_yearly(team_name, year_start, year_end)

# ---------------------------
# Sidebar
# ---------------------------
st.sidebar.header("Controls")

src = st.sidebar.selectbox(
    "Data source",
    options=["Clubs (ClubElo API)", "National Teams (eloratings.net)"],
    index=0
)

if src.startswith("Clubs"):
    st.sidebar.caption("Use the slug as it appears in ClubElo URLs (e.g., Valerenga, Rosenborg).")
    default_main = "Valerenga"
    main_label = "Main club (ClubElo slug)"
    compare_label = "Compare (up to 3 slugs, comma-separated)"
else:
    st.sidebar.caption("Type the national team name exactly as on eloratings (e.g., Norway, Brazil, Spain).")
    default_main = "Norway"
    main_label = "Main team (name)"
    compare_label = "Compare (up to 3 teams, comma-separated)"

team = st.sidebar.text_input(main_label, value=default_main)
compare_raw = st.sidebar.text_input(compare_label, value="")

date_min = st.sidebar.date_input("Start date", value=None)
date_max = st.sidebar.date_input("End date", value=None)

rolling = st.sidebar.number_input(
    "Moving average (entries)", min_value=0, max_value=50, value=0,
    help="0 = no smoothing. Applies per entity (club/team)."
)

index_mode = st.sidebar.checkbox("Show change since first date (Δ Elo)", value=True)

st.sidebar.subheader("Y-axis (log) — only when Δ is OFF")
use_custom_domain = st.sidebar.checkbox("Use custom log domain", value=True)
domain_slider = st.sidebar.slider(
    "Log domain [min, max]",
    min_value=500,
    max_value=4000,
    value=(900, 3000),
    step=50,
    help="Adjust to zoom vertically on log scale (requires Δ OFF).",
    disabled=index_mode
)

# ---------------------------
# Load data
# ---------------------------
all_series = []
errors = []

def load_one(entity: str):
    try:
        if src.startswith("Clubs"):
            return fetch_club_history(entity.strip())
        else:
            return fetch_selection_history(entity.strip(), prefer_graph_endpoint=True)
    except Exception as e:
        errors.append(f"Failed to fetch **{entity}**: {e}")
        return None

main_df = load_one(team)
if main_df is not None and not main_df.empty:
    all_series.append(main_df)

comp_list = [c.strip() for c in compare_raw.split(",") if c.strip()]
comp_list = comp_list[:3]
for ent in comp_list:
    cdf = load_one(ent)
    if cdf is not None and not cdf.empty:
        all_series.append(cdf)

if errors:
    st.warning(" • ".join(errors))

if not all_series:
    st.stop()

df = pd.concat(all_series, ignore_index=True)

# Date filters
if date_min:
    df = df[df["Date"] >= pd.to_datetime(date_min)]
if date_max:
    df = df[df["Date"] <= pd.to_datetime(date_max) + pd.to_timedelta(1, unit="D")]

# Smoothing
if rolling and rolling > 0:
    df = df.sort_values(["Entity", "Date"])
    df["Elo_smoothed"] = df.groupby("Entity")["Elo"].transform(lambda s: s.rolling(rolling, min_periods=1).mean())
    value_field = "Elo_smoothed"
else:
    value_field = "Elo"

# Index / delta
if index_mode:
    df = df.sort_values(["Entity", "Date"])
    first_vals = df.groupby("Entity")[value_field].transform("first")
    df["Delta"] = df[value_field] - first_vals
    plot_field = "Delta"
    y_title = "Δ Elo (vs first date)"
else:
    plot_field = value_field
    y_title = "Elo"

# ---------------------------
# Chart
# ---------------------------
if index_mode:
    y_enc = alt.Y(f"{plot_field}:Q", title=y_title)
else:
    log_scale = alt.Scale(type="log")
    if use_custom_domain and domain_slider:
        dom_min, dom_max = domain_slider
        dom_min = max(1, dom_min)
        dom_max = max(dom_min + 1, dom_max)
        log_scale = alt.Scale(type="log", domain=[dom_min, dom_max])
    y_enc = alt.Y(f"{plot_field}:Q", title=y_title, scale=log_scale)

base = alt.Chart(df).mark_line(interpolate="step-after").encode(
    x=alt.X("Date:T", title="Date"),
    y=y_enc,
    color=alt.Color("Entity:N", title="Club/Team"),
    tooltip=[
        alt.Tooltip("Entity:N", title="Entity"),
        alt.Tooltip("Date:T", title="Date"),
        alt.Tooltip("Elo:Q", title="Elo (raw)", format=".0f"),
        alt.Tooltip(f"{value_field}:Q", title="Elo (trace base)", format=".0f"),
        alt.Tooltip(f"{plot_field}:Q", title="Displayed value", format=".0f"),
    ],
).properties(height=460)

st.altair_chart(base.interactive(), use_container_width=True)

# ---------------------------
# Quick metrics (main entity)
# ---------------------------
st.subheader("Summary (main entity)")
m = df[df["Entity"] == team].copy()
if not m.empty:
    m = m.sort_values("Date")
    current = m.iloc[-1][value_field]
    start = m.iloc[0][value_field]
    delta = current - start
    col1, col2, col3 = st.columns(3)
    col1.metric("Latest Elo (in range)", f"{current:.0f}")
    col2.metric("First Elo (in range)", f"{start:.0f}")
    col3.metric("Change", f"{delta:+.0f}")
else:
    st.info("No data for the selected period.")

# ---------------------------
# Export
# ---------------------------
st.subheader("Export")
export_cols = ["Date", "Entity", "Elo"]
if "Elo_smoothed" in df.columns:
    export_cols.append("Elo_smoothed")
if "Delta" in df.columns:
    export_cols.append("Delta")

csv = df[export_cols].sort_values(["Entity", "Date"]).to_csv(index=False)
st.download_button("Download CSV (current view)", data=csv, file_name="elo_series.csv", mime="text/csv")

st.caption(
    """
Notes:
- Clubs: data from http://api.clubelo.com ([From, To] intervals = steps).
- National teams: tries chart endpoints first; otherwise, uses yearly snapshots (Dec 31) from eloratings.net and builds a step curve.
- Turn on “Δ Elo” to view changes (each entity is rebased to zero at the first date in the selected range).
- Log scale & custom domain only apply when Δ is OFF.
"""
)
