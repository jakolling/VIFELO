# app_en_nt.py — Robust parsing for eloratings.net yearly pages
import streamlit as st
import pandas as pd
import requests
from io import StringIO
import altair as alt
import re
from datetime import datetime
import html
import time

st.set_page_config(page_title="VIF Elo – Clubs & National Teams", layout="wide")

st.title("VIF — Elo Evolution (Clubs & National Teams)")
st.caption("Sources: api.clubelo.com (clubs) • eloratings.net (national teams)")

# ---------------------------
# Utilities
# ---------------------------
USER_AGENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

def _safe_get(url: str, timeout: int = 30) -> requests.Response:
    response = requests.get(url, timeout=timeout, headers=USER_AGENT_HEADERS)
    response.raise_for_status()
    return response

def _html_to_text(raw: str) -> str:
    """Strip HTML tags and unescape entities to get a single-line text blob."""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    collapsed = re.sub(r"\s+", " ", no_tags).strip()
    return html.unescape(collapsed)

def _stepify(df: pd.DataFrame, date_col: str, value_col: str, label_col: str, label_val: str) -> pd.DataFrame:
    """Convert point series to step-after style by duplicating each value at the next timestamp."""
    if df.empty:
        return df.copy()
    df_sorted = df.sort_values(date_col).reset_index(drop=True)
    rows = []
    for i, row in df_sorted.iterrows():
        d = row[date_col]
        v = row[value_col]
        rows.append({"Date": d, "Elo": v, "Entity": label_val})
        if i + 1 < len(df_sorted):
            d2 = df_sorted.loc[i + 1, date_col]
            rows.append({"Date": d2, "Elo": v, "Entity": label_val})
    return pd.DataFrame(rows)

# ---------------------------
# ClubElo (clubs)
# ---------------------------
@st.cache_data(show_spinner=False)
def fetch_club_history(club_slug: str) -> pd.DataFrame:
    """Fetch club Elo history from ClubElo API and return step series."""
    url = f"http://api.clubelo.com/{club_slug}"
    resp = _safe_get(url)
    df = pd.read_csv(StringIO(resp.text))

    # Parse dates & Elo
    for c in ["From", "To"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    if "Elo" in df.columns:
        df["Elo"] = pd.to_numeric(df["Elo"], errors="coerce")

    df = df.dropna(subset=["Elo"]).sort_values("From").reset_index(drop=True)

    # Build step data from From/To intervals
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

TEAM_ALIASES = {
    "Cote d'Ivoire": ["Côte d’Ivoire", "Cote d’Ivoire", "Ivory Coast"],
    "Bosnia and Herzegovina": ["Bosnia-Herzegovina", "Bosnia & Herzegovina"],
    "DR Congo": ["Congo DR", "Congo-Kinshasa"],
    "Congo": ["Congo-Brazzaville"],
    "South Korea": ["Korea Republic", "Korea, South", "Republic of Korea"],
    "North Korea": ["Korea DPR", "Korea, North", "DPR Korea"],
    "United States": ["USA", "United States of America"],
    "Iran": ["IR Iran", "Iran, Islamic Republic"],
    "Hong Kong": ["Hong-Kong"],
    "Moldova": ["Moldova, Republic of"],
    "Myanmar": ["Burma"],
    "Eswatini": ["Swaziland"],
    "Czech Republic": ["Czechia"],
    "Cape Verde": ["Cabo Verde"],
    "Timor-Leste": ["East Timor"],
    "Russia": ["Russian Federation"],
    "Syria": ["Syrian Arab Republic"],
    "Laos": ["Lao"],
}

def _name_matches(candidate: str, target: str) -> bool:
    """Case-insensitive match with alias support."""
    c = candidate.strip().lower()
    t = target.strip().lower()
    if c == t:
        return True
    for canon, alts in TEAM_ALIASES.items():
        canon_l = canon.lower()
        alts_l = [a.lower() for a in alts + [canon]]
        if c in alts_l and t in alts_l:
            return True
    return False


def _parse_year_snapshot(text_plain: str, team_name: str):
    """
    Parse a yearly snapshot (already HTML-stripped) and extract the rating for the given team.
    Accepts variants like: '1. Spain. 2178' or '1. Spain 2178'.
    """
    # Primary pattern: rank '.' name [optional '.'] rating
    pat = re.compile(r"\b\d+\.\s*([A-Za-zÀ-ÿ'’\-\.() &]+?)\s*\.?\s*([0-9]{3,4})\b")

    # Strict search first (exact team name via _name_matches)
    for m in pat.finditer(text_plain):
        name = m.group(1).strip()
        rating = m.group(2).strip()
        if _name_matches(name, team_name):
            try:
                return int(rating)
            except Exception:
                pass

    # Fallback: team name anywhere, followed by a trailing 3–4 digit rating within ~40 chars
    tail = re.compile(rf"{re.escape(team_name)}[^0-9]{{0,40}}([0-9]{{3,4}})\b", re.IGNORECASE)
    m2 = tail.search(text_plain)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            pass

    return None

@st.cache_data(show_spinner=False)
def fetch_national_history_yearly(team_name: str,
                                  year_start: int = YEARS_MIN,
                                  year_end: int = YEARS_MAX) -> pd.DataFrame:
    """
    Crawl yearly snapshots on eloratings.net and create an annual step series (Dec 31 each year).
    We fetch the HTML, strip tags, then parse as tolerant plain text.
    """
    records = []
    y0 = max(YEARS_MIN, year_start)
    y1 = min(YEARS_MAX, year_end)

    for y in range(y0, y1 + 1):
        url = f"https://www.eloratings.net/{y}"
        try:
            resp = _safe_get(url, timeout=20)
            txt_plain = _html_to_text(resp.text)
            elo_val = _parse_year_snapshot(txt_plain, team_name)
            if elo_val is not None:
                d = pd.Timestamp(f"{y}-12-31")
                records.append({"Date": d, "Elo": elo_val})
        except Exception:
            pass
        time.sleep(0.05)  # be polite

    if not records:
        raise ValueError(f"Could not find Elo for '{team_name}' in annual snapshots.")
    df = pd.DataFrame(records).sort_values("Date").reset_index(drop=True)
    return _stepify(df, "Date", "Elo", "Entity", team_name)


def _try_graph_endpoints(team_name: str) -> pd.DataFrame | None:
    """
    Try possible chart endpoints (if any are publicly available).
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
                s = ln.strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}\D+\d+(\.\d+)?$", s):
                    parts = re.split(r"[;, \t]+", s)
                    if len(parts) >= 2:
                        try:
                            d = pd.to_datetime(parts[0], errors="raise")
                            v = float(parts[1])
                            rows.append({"Date": d, "Elo": v})
                        except Exception:
                            pass
            if rows:
                df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
                return _stepify(df, "Date", "Elo", "Entity", team_name)
        except Exception:
            continue
    return None

@st.cache_data(show_spinner=False)
def fetch_selection_history(team_name: str,
                            prefer_graph_endpoint: bool = True,
                            year_start: int = YEARS_MIN,
                            year_end: int = YEARS_MAX) -> pd.DataFrame:
    """
    Retrieve Elo series for a national team.
    1) Try chart endpoints (if available).
    2) Fallback: yearly snapshots (Dec 31) with step curve.
    """
    if prefer_graph_endpoint:
        df = _try_graph_endpoints(team_name)
        if df is not None and not df.empty:
            return df
    return fetch_national_history_yearly(team_name, year_start, year_end)

# ---------------------------
# Sidebar (UI)
# ---------------------------
st.sidebar.header("Controls")

data_source = st.sidebar.selectbox(
    "Data source",
    options=["Clubs (ClubElo API)", "National Teams (eloratings.net)"],
    index=0
)

if data_source.startswith("Clubs"):
    st.sidebar.caption("Use the slug as it appears in ClubElo URLs (e.g., Valerenga, Rosenborg).")
    default_main = "Valerenga"
    main_label = "Main club (ClubElo slug)"
    compare_label = "Compare (up to 3 slugs, comma-separated)"
else:
    st.sidebar.caption("Type the national team name exactly as on eloratings (e.g., Norway, Brazil, Spain).")
    default_main = "Norway"
    main_label = "Main team (name)"
    compare_label = "Compare (up to 3 teams, comma-separated)"

entity_main = st.sidebar.text_input(main_label, value=default_main)
compare_raw = st.sidebar.text_input(compare_label, value="")

date_start = st.sidebar.date_input("Start date", value=None)
date_end = st.sidebar.date_input("End date", value=None)

moving_average_entries = st.sidebar.number_input(
    "Moving average (entries)", min_value=0, max_value=50, value=0,
    help="0 = no smoothing. Applied per entity (club/team)."
)

show_delta = st.sidebar.checkbox("Show change since first date (Δ Elo)", value=True)

st.sidebar.subheader("Y-axis (log) — only when Δ is OFF")
use_custom_log_domain = st.sidebar.checkbox("Use custom log domain", value=True)
log_domain = st.sidebar.slider(
    "Log domain [min, max]",
    min_value=500,
    max_value=4000,
    value=(900, 3000),
    step=50,
    help="Adjust to zoom vertically on log scale (requires Δ OFF).",
    disabled=show_delta
)

# ---------------------------
# Load data
# ---------------------------
all_series = []
errors = []

def _load_one(entity: str) -> pd.DataFrame | None:
    try:
        if data_source.startswith("Clubs"):
            return fetch_club_history(entity.strip())
        else:
            return fetch_selection_history(entity.strip(), prefer_graph_endpoint=True)
    except Exception as exc:
        errors.append(f"Failed to fetch **{entity}**: {exc}")
        return None

main_df = _load_one(entity_main)
if main_df is not None and not main_df.empty:
    all_series.append(main_df)

compare_entities = [c.strip() for c in compare_raw.split(",") if c.strip()]
compare_entities = compare_entities[:3]
for ent in compare_entities:
    cdf = _load_one(ent)
    if cdf is not None and not cdf.empty:
        all_series.append(cdf)

if errors:
    st.warning(" • ".join(errors))

if not all_series:
    st.stop()

df = pd.concat(all_series, ignore_index=True)

# Date filters
if date_start:
    df = df[df["Date"] >= pd.to_datetime(date_start)]
if date_end:
    df = df[df["Date"] <= pd.to_datetime(date_end) + pd.to_timedelta(1, unit="D")]

# Smoothing
if moving_average_entries and moving_average_entries > 0:
    df = df.sort_values(["Entity", "Date"])
    df["Elo_smoothed"] = df.groupby("Entity")["Elo"].transform(
        lambda s: s.rolling(int(moving_average_entries), min_periods=1).mean()
    )
    value_field = "Elo_smoothed"
else:
    value_field = "Elo"

# Delta / index mode
if show_delta:
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
if show_delta:
    y_enc = alt.Y(f"{plot_field}:Q", title=y_title)
else:
    log_scale = alt.Scale(type="log")
    if use_custom_log_domain and log_domain:
        dom_min, dom_max = log_domain
        dom_min = max(1, dom_min)
        dom_max = max(dom_min + 1, dom_max)
        log_scale = alt.Scale(type="log", domain=[dom_min, dom_max])
    y_enc = alt.Y(f"{plot_field}:Q", title=y_title, scale=log_scale)

chart = (
    alt.Chart(df)
    .mark_line(interpolate="step-after")
    .encode(
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
    )
    .properties(height=460)
)

st.altair_chart(chart.interactive(), use_container_width=True)

# ---------------------------
# Quick metrics (main entity)
# ---------------------------
st.subheader("Summary (main entity)")
main_mask = df["Entity"] == entity_main
m = df.loc[main_mask].copy()
if not m.empty:
    m = m.sort_values("Date")
    current = float(m.iloc[-1][value_field])
    start_val = float(m.iloc[0][value_field])
    delta_val = current - start_val
    col1, col2, col3 = st.columns(3)
    col1.metric("Latest Elo (in range)", f"{current:.0f}")
    col2.metric("First Elo (in range)", f"{start_val:.0f}")
    col3.metric("Change", f"{delta_val:+.0f}")
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

csv_bytes = df[export_cols].sort_values(["Entity", "Date"]).to_csv(index=False).encode("utf-8")
st.download_button("Download CSV (current view)", data=csv_bytes, file_name="elo_series.csv", mime="text/csv")

st.caption(
    """
Notes:
- Clubs: data from http://api.clubelo.com ([From, To] intervals = steps).
- National teams: tries chart endpoints first; otherwise, uses yearly snapshots (Dec 31) from eloratings.net and builds a step curve.
- Turn on “Δ Elo” to view changes (each entity is rebased to zero at the first date in the selected range).
- Log scale & custom domain only apply when Δ is OFF.
"""
)
