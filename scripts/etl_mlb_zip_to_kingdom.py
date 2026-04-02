#!/usr/bin/env python3
"""
ETL: MLB.zip → Three File Kingdom
Transforms historical MLB DFS data from the zip archive into:
  - data/historical_players.csv
  - data/historical_winning_drafts.csv
  - data/historical_slate_results.json

Usage (from repo root):
  python scripts/etl_mlb_zip_to_kingdom.py
"""

import csv
import io
import json
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ZIP_PATH = REPO / "MLB.zip"
DATA_DIR = REPO / "data"

# ---------------------------------------------------------------------------
# Player → standard position lookup (enrichment required by CLAUDE.md)
# ---------------------------------------------------------------------------
POSITIONS: dict[str, str] = {
    # Pitchers
    "Max Fried": "P",
    "Logan Webb": "P",
    "Carlos Rodon": "P",
    "Tarik Skubal": "P",
    "Paul Skenes": "P",
    "Yoshinobu Yamamoto": "P",
    "Garrett Crochet": "P",
    "Hunter Brown": "P",
    "Cristopher Sánchez": "P",
    "Cristopher Sanchez": "P",
    "Logan Gilbert": "P",
    "Jacob Misiorowski": "P",
    "Nathan Eovaldi": "P",
    "Trevor Rogers": "P",
    "Joe Ryan": "P",
    "José Soriano": "P",
    "Jose Soriano": "P",
    "Andrew Abbott": "P",
    "Kevin McGonigle": "P",
    "JT Brubaker": "P",
    "Jake Bird": "P",
    "Camilo Doval": "P",
    "Robbie Ray": "P",
    "George Kirby": "P",
    "Sandy Alcantara": "P",
    "Framber Valdez": "P",
    "Kevin Gausman": "P",
    "Cam Schlittler": "P",
    "Chris Sale": "P",
    "Michael King": "P",
    # Catchers
    "Austin Wells": "C",
    "Cal Raleigh": "C",
    "Shea Langeliers": "C",
    # First Base
    "Freddie Freeman": "1B",
    "Bryce Harper": "1B",
    "Ben Rice": "1B",
    # Second Base
    "Jose Altuve": "2B",
    "Andrés Giménez": "2B",
    "Andrés Gimenez": "2B",
    "Brandon Lowe": "2B",
    # Third Base
    "Rafael Devers": "3B",
    "Matt Chapman": "3B",
    "Ryan McMahon": "3B",
    "José Ramírez": "3B",
    "Jose Ramirez": "3B",
    "Jazz Chisholm Jr.": "3B",
    # Shortstop
    "Anthony Volpe": "SS",
    "Willy Adames": "SS",
    "Jose Caballero": "SS",
    "Fernando Tatis Jr.": "SS",
    # Outfield
    "Aaron Judge": "OF",
    "Mookie Betts": "OF",
    "Mike Trout": "OF",
    "Corbin Carroll": "OF",
    "Kyle Tucker": "OF",
    "Jung Hoo Lee": "OF",
    "Heliot Ramos": "OF",
    "Trent Grisham": "OF",
    "Cody Bellinger": "OF",
    "Andy Pages": "OF",
    "Chase DeLauter": "OF",
    "Alec Burleson": "OF",
    "Dominic Canzone": "OF",
    "Randy Arozarena": "OF",
    "Luke Raley": "OF",
    "Ramón Laureano": "OF",
    "Ramon Laureano": "OF",
    # DH
    "Giancarlo Stanton": "DH",
    "Yordan Alvarez": "DH",
    "Shohei Ohtani": "DH",
    "Luis Arraez": "DH",
}


def _norm_name(name: str) -> str:
    """Normalize player name: strip accents, lowercase, strip whitespace."""
    n = unicodedata.normalize("NFKD", name or "").encode("ASCII", "ignore").decode("ASCII")
    return n.strip().lower()


def _canonical_name(name: str, name_registry: dict[str, str]) -> str:
    """Return canonical (accented) name if already seen, else register and return as-is."""
    key = _norm_name(name)
    if key not in name_registry:
        name_registry[key] = name.strip()
    return name_registry[key]


def _float_or_empty(val) -> str:
    if val is None or str(val).strip() == "":
        return ""
    try:
        return str(float(str(val).strip()))
    except ValueError:
        return ""


def _compute_total_value(real_score: str, card_boost: str) -> str:
    """total_value = real_score * (BASE_MULTIPLIER + card_boost). Returns '' if real_score missing."""
    from app.core.utils import BASE_MULTIPLIER
    if real_score == "":
        return ""
    try:
        rs = float(real_score)
        cb = float(card_boost) if card_boost != "" else 0.0
        return str(round(rs * (BASE_MULTIPLIER + cb), 2))
    except ValueError:
        return ""


def read_zip_csv(zf: zipfile.ZipFile, path: str) -> list[dict]:
    with zf.open(path) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        return [row for row in reader]


def read_zip_json(zf: zipfile.ZipFile, path: str) -> dict:
    with zf.open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Per-date processing
# ---------------------------------------------------------------------------

class SlateData:
    def __init__(self, date: str):
        self.date = date
        # player_key → dict with all known fields
        self.players: dict[str, dict] = {}
        self.winning_drafts: list[dict] = []
        self.name_registry: dict[str, str] = {}

    def _get_or_create_player(self, name: str, team: str) -> dict:
        key = _norm_name(name)
        canonical = _canonical_name(name, self.name_registry)
        if key not in self.players:
            self.players[key] = {
                "date": self.date,
                "player_name": canonical,
                "team": team.strip().upper() if team else "",
                "position": POSITIONS.get(canonical, POSITIONS.get(name.strip(), "")),
                "real_score": "",
                "card_boost": "",
                "drafts": "",
                "total_value": "",
                "is_highest_value": "0",
                "is_most_popular": "0",
                "is_most_drafted_3x": "0",
            }
        else:
            # Update team if missing
            if not self.players[key]["team"] and team:
                self.players[key]["team"] = team.strip().upper()
            # Update position if missing
            if not self.players[key]["position"]:
                pos = POSITIONS.get(canonical, POSITIONS.get(name.strip(), ""))
                if pos:
                    self.players[key]["position"] = pos
        return self.players[key]

    def load_actuals(self, rows: list[dict]):
        """Load from actuals file: player_name,team,actual_rs,actual_card_boost,drafts,...,source"""
        for r in rows:
            name = (r.get("player_name") or "").strip()
            if not name:
                continue
            team = r.get("team", "")
            p = self._get_or_create_player(name, team)
            rs = _float_or_empty(r.get("actual_rs", ""))
            cb = _float_or_empty(r.get("actual_card_boost", ""))
            drafts = _float_or_empty(r.get("drafts", ""))
            source = (r.get("source") or "").strip().lower()

            if rs and not p["real_score"]:
                p["real_score"] = rs
            if cb and not p["card_boost"]:
                p["card_boost"] = cb
            if drafts and not p["drafts"]:
                p["drafts"] = drafts

            if "highest_value" in source:
                p["is_highest_value"] = "1"
            if "most_popular" in source:
                p["is_most_popular"] = "1"

            p["total_value"] = _compute_total_value(p["real_score"], p["card_boost"])

    def load_most_popular(self, rows: list[dict]):
        """Load from most_popular file. Handles two schemas:
        Schema A: player_name,team,actual_rs,draft_count,actual_card_boost,avg_finish
        Schema B: player,team,draft_count,actual_rs,actual_card_boost,avg_finish,rank,saved_at
        """
        for r in rows:
            name = (r.get("player_name") or r.get("player") or "").strip()
            if not name:
                continue
            team = r.get("team", "")
            p = self._get_or_create_player(name, team)

            rs = _float_or_empty(r.get("actual_rs", ""))
            cb = _float_or_empty(r.get("actual_card_boost", ""))
            drafts = _float_or_empty(r.get("draft_count", "") or r.get("drafts", ""))

            if rs and not p["real_score"]:
                p["real_score"] = rs
            if cb and not p["card_boost"]:
                p["card_boost"] = cb
            if drafts and not p["drafts"]:
                p["drafts"] = drafts

            p["is_most_popular"] = "1"
            p["total_value"] = _compute_total_value(p["real_score"], p["card_boost"])

    def load_most_drafted_3x(self, rows: list[dict]):
        """Load from most_drafted_3x file. Schema B: player,team,draft_count,..."""
        for r in rows:
            name = (r.get("player_name") or r.get("player") or "").strip()
            if not name:
                continue
            team = r.get("team", "")
            p = self._get_or_create_player(name, team)

            rs = _float_or_empty(r.get("actual_rs", ""))
            cb = _float_or_empty(r.get("actual_card_boost", ""))
            drafts = _float_or_empty(r.get("draft_count", "") or r.get("drafts", ""))

            if rs and not p["real_score"]:
                p["real_score"] = rs
            if cb and not p["card_boost"]:
                p["card_boost"] = cb
            if drafts and not p["drafts"]:
                p["drafts"] = drafts

            p["is_most_drafted_3x"] = "1"
            p["total_value"] = _compute_total_value(p["real_score"], p["card_boost"])

    def load_winning_drafts(self, rows: list[dict]):
        """Load from winning_drafts file."""
        for r in rows:
            name = (r.get("player_name") or "").strip()
            if not name:
                continue
            canonical = _canonical_name(name, self.name_registry)
            team = (r.get("team") or "").strip().upper()
            position = POSITIONS.get(canonical, POSITIONS.get(name, ""))
            self.winning_drafts.append({
                "date": self.date,
                "winner_rank": (r.get("winner_rank") or "").strip(),
                "slot_index": (r.get("slot_index") or "").strip(),
                "player_name": canonical,
                "team": team,
                "position": position,
                "real_score": _float_or_empty(r.get("actual_rs", "")),
                "slot_mult": _float_or_empty(r.get("slot_mult", "")),
                "card_boost": _float_or_empty(r.get("card_boost", "")),
            })

    def player_rows(self) -> list[dict]:
        return list(self.players.values())


# ---------------------------------------------------------------------------
# Main ETL
# ---------------------------------------------------------------------------

def main():
    all_players: list[dict] = []
    all_winning_drafts: list[dict] = []
    slate_results: list[dict] = []

    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        available = set(zf.namelist())

        # -------------------------------------------------------------------
        # 2026-03-25: Opening Day 1 game (NYY vs SF)
        # Source files are mislabeled as 2025 in data/mlb/ namespace
        # -------------------------------------------------------------------
        s25 = SlateData("2026-03-25")

        actuals_25 = read_zip_csv(zf, "data/mlb/actuals/2025-03-25.csv")
        s25.load_actuals(actuals_25)

        most_pop_25 = read_zip_csv(zf, "data/mlb/most_popular/2025-03-25.csv")
        s25.load_most_popular(most_pop_25)

        winning_25 = read_zip_csv(zf, "data/mlb/winning_drafts/2025-03-25.csv")
        s25.load_winning_drafts(winning_25)

        slate_25 = read_zip_json(zf, "data/mlb/slate_results/2025-03-25.json")
        slate_25["date"] = "2026-03-25"
        slate_results.append(slate_25)

        all_players.extend(s25.player_rows())
        all_winning_drafts.extend(s25.winning_drafts)

        # -------------------------------------------------------------------
        # 2026-03-26: Opening Day large slate (15 games)
        # Use canonical data/ namespace files
        # -------------------------------------------------------------------
        s26 = SlateData("2026-03-26")

        actuals_26 = read_zip_csv(zf, "data/actuals/2026-03-26.csv")
        s26.load_actuals(actuals_26)

        most_pop_26 = read_zip_csv(zf, "data/most_popular/2026-03-26-mlb.csv")
        s26.load_most_popular(most_pop_26)

        winning_26 = read_zip_csv(zf, "data/winning_drafts/2026-03-26-mlb.csv")
        s26.load_winning_drafts(winning_26)

        slate_26 = read_zip_json(zf, "data/mlb/slate_results/2025-03-26.json")
        slate_26["date"] = "2026-03-26"
        slate_results.append(slate_26)

        all_players.extend(s26.player_rows())
        all_winning_drafts.extend(s26.winning_drafts)

        # -------------------------------------------------------------------
        # 2026-03-27: Next slate
        # -------------------------------------------------------------------
        s27 = SlateData("2026-03-27")

        actuals_27 = read_zip_csv(zf, "data/actuals/2026-03-27.csv")
        s27.load_actuals(actuals_27)

        most_pop_27 = read_zip_csv(zf, "data/most_popular/2026-03-27.csv")
        s27.load_most_popular(most_pop_27)

        most_3x_27 = read_zip_csv(zf, "data/most_drafted_3x/2026-03-27.csv")
        s27.load_most_drafted_3x(most_3x_27)

        winning_27 = read_zip_csv(zf, "data/winning_drafts/2026-03-27.csv")
        s27.load_winning_drafts(winning_27)

        # No slate_results JSON for 2026-03-27 in the zip

        all_players.extend(s27.player_rows())
        all_winning_drafts.extend(s27.winning_drafts)

    # -----------------------------------------------------------------------
    # Write historical_players.csv
    # -----------------------------------------------------------------------
    players_path = DATA_DIR / "historical_players.csv"
    player_fields = [
        "date", "player_name", "team", "position", "real_score",
        "card_boost", "drafts", "total_value",
        "is_highest_value", "is_most_popular", "is_most_drafted_3x",
    ]
    with open(players_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=player_fields)
        writer.writeheader()
        writer.writerows(all_players)
    print(f"Wrote {len(all_players)} player rows → {players_path}")

    # -----------------------------------------------------------------------
    # Write historical_winning_drafts.csv
    # -----------------------------------------------------------------------
    drafts_path = DATA_DIR / "historical_winning_drafts.csv"
    draft_fields = [
        "date", "winner_rank", "slot_index", "player_name", "team",
        "position", "real_score", "slot_mult", "card_boost",
    ]
    with open(drafts_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=draft_fields)
        writer.writeheader()
        writer.writerows(all_winning_drafts)
    print(f"Wrote {len(all_winning_drafts)} winning draft rows → {drafts_path}")

    # -----------------------------------------------------------------------
    # Write historical_slate_results.json
    # -----------------------------------------------------------------------
    results_path = DATA_DIR / "historical_slate_results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(slate_results, f, indent=2)
    print(f"Wrote {len(slate_results)} slate entries → {results_path}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n=== ETL Summary ===")
    dates = sorted(set(p["date"] for p in all_players))
    for d in dates:
        day_players = [p for p in all_players if p["date"] == d]
        hv = sum(1 for p in day_players if p["is_highest_value"] == "1")
        mp = sum(1 for p in day_players if p["is_most_popular"] == "1")
        m3 = sum(1 for p in day_players if p["is_most_drafted_3x"] == "1")
        wd = len([r for r in all_winning_drafts if r["date"] == d])
        print(f"  {d}: {len(day_players)} players (HV={hv}, MP={mp}, 3x={m3}), {wd} draft slots")

    missing_pos = [p for p in all_players if not p["position"]]
    if missing_pos:
        print(f"\n  WARNING: {len(missing_pos)} players with no position:")
        for p in missing_pos:
            print(f"    {p['date']} | {p['player_name']} | {p['team']}")


if __name__ == "__main__":
    main()
