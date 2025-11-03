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
    Parse a yearly snapshot (already HTML-stripped) and extract the rating
    for the given team. Accepts variants like:
      '1. Spain. 2178' or '1. Spain 2178'
    """
    # Primary pattern: rank '.' name [optional '.'] rating
    pat = re.compile(r"\b\d+\.\s*([A-Za-zÀ-ÿ'’\-\.\(\) &]+?)\s*\.?\s*([0-9]{3,4})\b")

    # Strict search first (exact team name via _name_matches)
    for m in pat.finditer(text_plain):
        name = m.group(1).strip()
        rating = m.group(2).strip()
        if _name_matches(name, team_name):
            try:
                return int(rating)
            except Exception:
                pass

    # Fallback: team name anywhere, followed by a trailing 3–4 digit rating
    # e.g., "... Norway ... 1850"
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
                            pass\n            if rows:\n                df = pd.DataFrame(rows).sort_values(\"Date\").reset_index(drop=True)\n                return _stepify(df, \"Date\", \"Elo\", \"Entity\", team_name)\n        except Exception:\n            continue\n    return None\n\n@st.cache_data(show_spinner=False)\ndef fetch_selection_history(team_name: str,\n                            prefer_graph_endpoint: bool = True,\n                            year_start: int = YEARS_MIN,\n                            year_end: int = YEARS_MAX) -> pd.DataFrame:\n    \"\"\"\n    Retrieve Elo series for a national team.\n    1) Try chart endpoints (if available).\n    2) Fallback: yearly snapshots (Dec 31) with step curve.\n    \"\"\"\n    if prefer_graph_endpoint:\n        df = _try_graph_endpoints(team_name)\n        if df is not None and not df.empty:\n            return df\n    return fetch_national_history_yearly(team_name, year_start, year_end)\n\n# ---------------------------\n# Sidebar (UI)\n# ---------------------------\nst.sidebar.header(\"Controls\")\n\ndata_source = st.sidebar.selectbox(\n    \"Data source\",\n    options=[\"Clubs (ClubElo API)\", \"National Teams (eloratings.net)\"],\n    index=0\n)\n\nif data_source.startswith(\"Clubs\"):\n    st.sidebar.caption(\"Use the slug as it appears in ClubElo URLs (e.g., Valerenga, Rosenborg).\")\n    default_main = \"Valerenga\"\n    main_label = \"Main club (ClubElo slug)\"\n    compare_label = \"Compare (up to 3 slugs, comma-separated)\"\nelse:\n    st.sidebar.caption(\"Type the national team name exactly as on eloratings (e.g., Norway, Brazil, Spain).\")\n    default_main = \"Norway\"\n    main_label = \"Main team (name)\"\n    compare_label = \"Compare (up to 3 teams, comma-separated)\"\n\nentity_main = st.sidebar.text_input(main_label, value=default_main)\ncompare_raw = st.sidebar.text_input(compare_label, value=\"\")\n\ndate_start = st.sidebar.date_input(\"Start date\", value=None)\ndate_end = st.sidebar.date_input(\"End date\", value=None)\n\nmoving_average_entries = st.sidebar.number_input(\n    \"Moving average (entries)\", min_value=0, max_value=50, value=0,\n    help=\"0 = no smoothing. Applied per entity (club/team).\"\n)\n\nshow_delta = st.sidebar.checkbox(\"Show change since first date (Δ Elo)\", value=True)\n\nst.sidebar.subheader(\"Y-axis (log) — only when Δ is OFF\")\nuse_custom_log_domain = st.sidebar.checkbox(\"Use custom log domain\", value=True)\nlog_domain = st.sidebar.slider(\n    \"Log domain [min, max]\",\n    min_value=500,\n    max_value=4000,\n    value=(900, 3000),\n    step=50,\n    help=\"Adjust to zoom vertically on log scale (requires Δ OFF).\",\n    disabled=show_delta\n)\n\n# ---------------------------\n# Load data\n# ---------------------------\nall_series = []\nerrors = []\n\ndef _load_one(entity: str) -> pd.DataFrame | None:\n    try:\n        if data_source.startswith(\"Clubs\"):\n            return fetch_club_history(entity.strip())\n        else:\n            return fetch_selection_history(entity.strip(), prefer_graph_endpoint=True)\n    except Exception as exc:\n        errors.append(f\"Failed to fetch **{entity}**: {exc}\")\n        return None\n\nmain_df = _load_one(entity_main)\nif main_df is not None and not main_df.empty:\n    all_series.append(main_df)\n\ncompare_entities = [c.strip() for c in compare_raw.split(\",\") if c.strip()]\ncompare_entities = compare_entities[:3]\nfor ent in compare_entities:\n    cdf = _load_one(ent)\n    if cdf is not None and not cdf.empty:\n        all_series.append(cdf)\n\nif errors:\n    st.warning(\" • \".join(errors))\n\nif not all_series:\n    st.stop()\n\ndf = pd.concat(all_series, ignore_index=True)\n\n# Date filters\nif date_start:\n    df = df[df[\"Date\"] >= pd.to_datetime(date_start)]\nif date_end:\n    df = df[df[\"Date\"] <= pd.to_datetime(date_end) + pd.to_timedelta(1, unit=\"D\")]\n\n# Smoothing\nif moving_average_entries and moving_average_entries > 0:\n    df = df.sort_values([\"Entity\", \"Date\"])\n    df[\"Elo_smoothed\"] = df.groupby(\"Entity\")[\"Elo\"].transform(\n        lambda s: s.rolling(int(moving_average_entries), min_periods=1).mean()\n    )\n    value_field = \"Elo_smoothed\"\nelse:\n    value_field = \"Elo\"\n\n# Delta / index mode\nif show_delta:\n    df = df.sort_values([\"Entity\", \"Date\"])\n    first_vals = df.groupby(\"Entity\")[value_field].transform(\"first\")\n    df[\"Delta\"] = df[value_field] - first_vals\n    plot_field = \"Delta\"\n    y_title = \"Δ Elo (vs first date)\"\nelse:\n    plot_field = value_field\n    y_title = \"Elo\"\n\n# ---------------------------\n# Chart\n# ---------------------------\nif show_delta:\n    y_enc = alt.Y(f\"{plot_field}:Q\", title=y_title)\nelse:\n    log_scale = alt.Scale(type=\"log\")\n    if use_custom_log_domain and log_domain:\n        dom_min, dom_max = log_domain\n        dom_min = max(1, dom_min)\n        dom_max = max(dom_min + 1, dom_max)\n        log_scale = alt.Scale(type=\"log\", domain=[dom_min, dom_max])\n    y_enc = alt.Y(f\"{plot_field}:Q\", title=y_title, scale=log_scale)\n\nchart = (\n    alt.Chart(df)\n    .mark_line(interpolate=\"step-after\")\n    .encode(\n        x=alt.X(\"Date:T\", title=\"Date\"),\n        y=y_enc,\n        color=alt.Color(\"Entity:N\", title=\"Club/Team\"),\n        tooltip=[\n            alt.Tooltip(\"Entity:N\", title=\"Entity\"),\n            alt.Tooltip(\"Date:T\", title=\"Date\"),\n            alt.Tooltip(\"Elo:Q\", title=\"Elo (raw)\", format=\".0f\"),\n            alt.Tooltip(f\"{value_field}:Q\", title=\"Elo (trace base)\", format=\".0f\"),\n            alt.Tooltip(f\"{plot_field}:Q\", title=\"Displayed value\", format=\".0f\"),\n        ],\n    )\n    .properties(height=460)\n)\n\nst.altair_chart(chart.interactive(), use_container_width=True)\n\n# ---------------------------\n# Quick metrics (main entity)\n# ---------------------------\nst.subheader(\"Summary (main entity)\")\nmain_mask = df[\"Entity\"] == entity_main\nm = df.loc[main_mask].copy()\nif not m.empty:\n    m = m.sort_values(\"Date\")\n    current = float(m.iloc[-1][value_field])\n    start_val = float(m.iloc[0][value_field])\n    delta_val = current - start_val\n    col1, col2, col3 = st.columns(3)\n    col1.metric(\"Latest Elo (in range)\", f\"{current:.0f}\")\n    col2.metric(\"First Elo (in range)\", f\"{start_val:.0f}\")\n    col3.metric(\"Change\", f\"{delta_val:+.0f}\")\nelse:\n    st.info(\"No data for the selected period.\")\n\n# ---------------------------\n# Export\n# ---------------------------\nst.subheader(\"Export\")\nexport_cols = [\"Date\", \"Entity\", \"Elo\"]\nif \"Elo_smoothed\" in df.columns:\n    export_cols.append(\"Elo_smoothed\")\nif \"Delta\" in df.columns:\n    export_cols.append(\"Delta\")\n\ncsv_bytes = df[export_cols].sort_values([\"Entity\", \"Date\"]).to_csv(index=False).encode(\"utf-8\")\nst.download_button(\"Download CSV (current view)\", data=csv_bytes, file_name=\"elo_series.csv\", mime=\"text/csv\")\n\nst.caption(\n    \"\"\"\nNotes:\n- Clubs: data from http://api.clubelo.com ([From, To] intervals = steps).\n- National teams: tries chart endpoints first; otherwise, uses yearly snapshots (Dec 31) from eloratings.net and builds a step curve.\n- Turn on “Δ Elo” to view changes (each entity is rebased to zero at the first date in the selected range).\n- Log scale & custom domain only apply when Δ is OFF.\n\"\"\"\n)\n
