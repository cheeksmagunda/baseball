"""
Apply corrections to hv_player_game_stats.csv:
- Fix wrong team_actual values (players traded/signed since initial data entry)
- Fix wrong position values (e.g., pitchers listed as OF)
- Fix wrong game_result values
- Fill in missing stats from web research
"""

import csv, copy, io

CSV_PATH = "/home/user/baseball/data/hv_player_game_stats.csv"

COLS = [
    "date", "player_name", "team_actual", "position", "real_score", "card_boost",
    "game_result", "ab", "r", "h", "hr", "rbi", "bb", "so",
    "ip", "er", "k_pitching", "decision", "notes"
]

def key(row):
    return (row["date"], row["player_name"])

# Each entry: (date, player_name) -> dict of field->new_value
# Only the specified fields are updated; others are left unchanged.
UPDATES = {
    # ─── APR 9 ───
    ("2026-04-09", "Max Muncy"): {
        "notes": "No LAD game on Apr 9 (off day) — DNP"
    },

    # ─── APR 14 ───
    ("2026-04-14", "Mick Abel"): {
        "team_actual": "MIN",
        "game_result": "BOS 0 MIN 6",
        "ip": "7.0", "er": "0.0", "k_pitching": "10", "decision": "W",
        "notes": "7 IP 0 ER 10K W | vs BOS (home)"
    },
    ("2026-04-14", "Dominic Smith"): {
        "team_actual": "ATL",
        "game_result": "ATL 6 MIA 5",
        "ab": "4.0", "r": "1.0", "h": "3.0", "hr": "0.0", "rbi": "4.0",
        "notes": "3-for-4 | RBI single in 2nd + bases-clearing 2B in 8th | vs MIA"
    },
    ("2026-04-14", "Ildemaro Vargas"): {
        "team_actual": "ARI",
        "notes": "Team correction: ARI (signed Dec 2025) — game/stats pending MLB API"
    },
    ("2026-04-14", "Ben Williamson"): {
        "team_actual": "TB",
        "game_result": "TB 8 CWS 5",
        "notes": "Team correction: TB — stats pending MLB API"
    },
    ("2026-04-14", "Everson Pereira"): {
        "team_actual": "CWS",
        "game_result": "TB 8 CWS 5",
        "notes": "Team correction: CWS — stats pending MLB API"
    },
    ("2026-04-14", "Munetaka Murakami"): {
        "team_actual": "CWS",
        "game_result": "TB 8 CWS 5",
        "notes": "Team correction: CWS — stats pending MLB API"
    },
    ("2026-04-14", "Ryan Vilade"): {
        "team_actual": "TB",
        "game_result": "TB 8 CWS 5",
        "notes": "Team correction: TB — stats pending MLB API"
    },

    # ─── APR 15 ───
    ("2026-04-15", "Shohei Ohtani"): {
        "position": "P",
        "game_result": "NYM 2 LAD 8",
        # Clear batting cols — pitcher only start (shoulder soreness)
        "ab": "", "r": "", "h": "", "hr": "", "rbi": "", "bb": "", "so": "",
        "ip": "6.0", "er": "1.0", "k_pitching": "10", "decision": "W",
        "notes": "6 IP 1 ER 10K W | pitcher-only start (hit in shoulder prev game) | vs NYM (home)"
    },
    ("2026-04-15", "Jeremiah Jackson"): {
        "team_actual": "BAL",
        "game_result": "AZ 8 BAL 5",
        "notes": "Team correction: BAL — stats pending MLB API"
    },
    ("2026-04-15", "José Caballero"): {
        "team_actual": "NYY",
        "game_result": "LAA 4 NYY 5",
        "notes": "Team correction: NYY — stats pending MLB API"
    },
    ("2026-04-15", "Jake Burger"): {
        "team_actual": "TEX",
        "game_result": "TEX 5 ATH 6",
        "notes": "Team correction: TEX — stats pending MLB API"
    },
    ("2026-04-15", "Ryan Kreidler"): {
        "team_actual": "MIN",
        "game_result": "BOS 9 MIN 5",
        "h": "1.0", "hr": "1.0", "rbi": "3.0",
        "notes": "3-run HR in 9th (384ft 104.1mph) cutting deficit to 9-5 | vs BOS (home)"
    },

    # ─── APR 16 ───
    ("2026-04-16", "Parker Messick"): {
        "team_actual": "CLE",
        "game_result": "BAL 2 CLE 4",
        "ip": "8.1", "er": "0.0", "k_pitching": "9", "decision": "W",
        "notes": "8.1 IP 0 ER 9K W | no-hit bid broken up in 9th by Taveras | vs BAL (home)"
    },
    ("2026-04-16", "Marcell Ozuna"): {
        "team_actual": "PIT",
        "game_result": "WSH 8 PIT 7",
        "h": "1.0", "hr": "1.0", "rbi": "3.0", "bb": "1.0",
        "notes": "3-run HR tying game 4-4 in 5th (423ft 109.6mph) + BB | vs WSH (home)"
    },
    ("2026-04-16", "Everson Pereira"): {
        "team_actual": "CWS",
        "game_result": "TB 5 CWS 3",
        "notes": "Team correction: CWS — stats pending MLB API"
    },

    # ─── APR 17 ───
    ("2026-04-17", "Munetaka Murakami"): {
        "team_actual": "CWS",
        "game_result": "CWS 9 ATH 2",
        "hr": "1.0", "rbi": "4.0",
        "notes": "grand slam in 1st inning | 4 RBI | vs ATH (away)"
    },
    ("2026-04-17", "Otto Lopez"): {
        "team_actual": "MIA",
        "game_result": "MIL 7 MIA 5",
        "h": "2.0", "hr": "1.0", "rbi": "2.0",
        "notes": "triple in 4th + 2-run HR in 6th (3rd HR) | vs MIL (home)"
    },
    ("2026-04-17", "Moises Ballesteros"): {
        "team_actual": "CHC",
        "game_result": "NYM 4 CHC 12",
        "hr": "1.0", "rbi": "3.0",
        "notes": "3-run HR in 1st (364ft 96.3mph EV) | vs NYM (home)"
    },
    ("2026-04-17", "Jeremiah Jackson"): {
        "team_actual": "BAL",
        "game_result": "BAL 6 CLE 4",
        "hr": "1.0", "rbi": "3.0",
        "notes": "3-run HR capping 6-run 8th inning | vs CLE (away)"
    },
    ("2026-04-17", "Ranger Suarez"): {
        "team_actual": "BOS",
        "game_result": "DET 0 BOS 1",
        "ip": "8.0", "er": "0.0", "k_pitching": "4", "decision": "ND",
        "notes": "8 IP 0 ER 4K ND | walk-off BOS win in extras | vs DET (home)"
    },
    ("2026-04-17", "Matt Chapman"): {
        "team_actual": "SF",
        "game_result": "SF 10 WSH 5",
        "h": "3.0", "rbi": "3.0",
        "notes": "3-hit game | 3 RBI (7th multi-hit game of season) | vs WSH (away)"
    },
    ("2026-04-17", "Daniel Schneemann"): {
        "team_actual": "CLE",
        "game_result": "BAL 6 CLE 4",
        "ab": "4.0", "h": "2.0", "hr": "1.0", "rbi": "4.0",
        "notes": "2-for-4 | grand slam (407ft) in 7th | 4 RBI | vs BAL (home)"
    },
    ("2026-04-17", "Spencer Horwitz"): {
        "team_actual": "PIT",
        "game_result": "TB 1 PIT 5",
        "ab": "3.0", "h": "3.0", "rbi": "1.0",
        "notes": "3-for-3 | RBI double (perfect 10-for-10 career vs Martinez) | vs TB (home)"
    },
    ("2026-04-17", "Nolan Arenado"): {
        "team_actual": "ARI",
        "game_result": "TOR 3 ARI 6",
        "hr": "1.0", "rbi": "2.0",
        "notes": "HR in 4th + RBI in 7th | 2 RBI | vs TOR (home)"
    },
    ("2026-04-17", "Luisangel Acuna"): {
        "team_actual": "CWS",
        "game_result": "CWS 9 ATH 2",
        "rbi": "2.0",
        "notes": "RBI double in 5th + 2 RBI + SB (5th of season) | vs ATH (away)"
    },

    # ─── APR 19 ───
    ("2026-04-19", "Walbert Urena"): {
        "position": "P",
        "game_result": "SD 2 LAA 1",
        "ip": "6.0", "er": "2.0", "k_pitching": "8", "decision": "L",
        "notes": "6 IP 2 ER 8K L | vs SD (home)"
    },
    ("2026-04-19", "Edouard Julien"): {
        "team_actual": "COL",
        "game_result": "LAD 6 COL 9",
        "h": "3.0", "rbi": "3.0",
        "notes": "leadoff 2B + 2-run single in 8th | 3-hit game | vs LAD (home)"
    },

    # ─── APR 20 ───
    ("2026-04-20", "Isaac Paredes"): {
        "team_actual": "HOU",
        "game_result": "HOU 9 CLE 2",
        "ab": "5.0", "h": "3.0", "hr": "2.0", "rbi": "2.0",
        "notes": "3-for-5 | 2 solo HR (1st 2 HRs of 2026 season) | vs CLE (away)"
    },
    ("2026-04-20", "Leody Taveras"): {
        "team_actual": "BAL",
        "game_result": "BAL 7 KC 5",
        "hr": "1.0", "rbi": "4.0",
        "notes": "grand slam in 12th inning | 4 RBI | vs KC (away)"
    },
    ("2026-04-20", "Carlos Cortes"): {
        "team_actual": "ATH",
        "game_result": "ATH 6 SEA 4",
        "ab": "5.0", "h": "4.0", "hr": "1.0",
        "notes": "4-for-5 | solo HR | vs SEA (away)"
    },
    ("2026-04-20", "Josh Naylor"): {
        "team_actual": "SEA",
        "game_result": "ATH 6 SEA 4",
        "ab": "4.0", "h": "3.0", "rbi": "1.0",
        "notes": "3-for-4 | RBI double in 1st | vs ATH (home)"
    },
    ("2026-04-20", "Carlos Correa"): {
        "team_actual": "HOU",
        "game_result": "HOU 9 CLE 2",
        "notes": "Team correction: HOU — stats pending MLB API"
    },

    # ─── APR 21 ───
    ("2026-04-21", "Elly De La Cruz"): {
        "ab": "6.0", "h": "3.0", "hr": "2.0", "rbi": "5.0",
        "notes": "3-for-6 | 2 HR (2-run in 1st + solo in 9th) | 5 RBI | 6th career multi-HR game | vs TB (home)"
    },
    ("2026-04-21", "Luis Garcia Jr."): {
        "h": "4.0", "rbi": "3.0",
        "notes": "4-hit game | 2-run 2B in 7th + RBI single in 5th | vs ATL (home)"
    },
    ("2026-04-21", "Luis Gil"): {
        "game_result": "NYY 4 BOS 0",
        "ip": "6.1", "er": "0.0", "k_pitching": "2", "decision": "W",
        "notes": "6.1 IP 0 ER W (shutout) | vs BOS (away)"
    },
    ("2026-04-21", "Giancarlo Stanton"): {
        "game_result": "NYY 4 BOS 0",
        "hr": "1.0",
        "notes": "2 XBH incl HR (111.5mph 41deg) | broke 1-for-21 slump | vs BOS (away)"
    },
    ("2026-04-21", "Chase DeLauter"): {
        "rbi": "3.0",
        "notes": "go-ahead 3-run triple vs Astros | 3 RBI | vs HOU (home)"
    },
    ("2026-04-21", "Colson Montgomery"): {
        "hr": "1.0",
        "notes": "HR (back-to-back-to-back in 2nd with Vargas+Murakami) | vs ARI (home)"
    },
    ("2026-04-21", "Adley Rutschman"): {
        "hr": "1.0", "rbi": "2.0",
        "notes": "2-run go-ahead HR in 8th | vs KC (home)"
    },
    ("2026-04-21", "Coby Mayo"): {
        "hr": "1.0", "rbi": "3.0",
        "notes": "3-run HR in 2nd for 3-0 BAL lead | vs KC (home)"
    },
    ("2026-04-21", "Jakob Marsee"): {
        "h": "3.0", "hr": "1.0",
        "notes": "3-for-4 | HR (1st MLB HR, first swing with new bat) | vs STL (away)"
    },
    ("2026-04-21", "Luke Keaschall"): {
        "rbi": "2.0",
        "notes": "2 RBI singles incl go-ahead hit in 9th | vs NYM (home)"
    },
    ("2026-04-21", "Nathan Church"): {
        "hr": "1.0", "rbi": "2.0",
        "notes": "2-run HR in 4th (370ft) | vs MIA (away)"
    },
    ("2026-04-21", "Sam Antonacci"): {
        "rbi": "3.0",
        "notes": "triple (1st MLB RBI) + inside-park HR in 9th (1st MLB HR) | 3 RBI | vs ARI (home)"
    },
    ("2026-04-21", "Curtis Mead"): {
        "hr": "1.0", "rbi": "3.0",
        "notes": "3-run HR in 8th | 3 RBI | vs ATL (home)"
    },
    ("2026-04-21", "Alec Burleson"): {
        "rbi": "2.0",
        "notes": "2-run double in 5th (+ Gorman RBI single) | vs MIA (away)"
    },
    ("2026-04-21", "Munetaka Murakami"): {
        "h": "2.0", "hr": "1.0",
        "notes": "single + HR (426ft, 9th of season, 4th straight game with HR) | vs ARI (home)"
    },
}

def apply_updates():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    changed = 0
    for row in rows:
        k = (row["date"], row["player_name"])
        if k in UPDATES:
            updates = UPDATES[k]
            for field, val in updates.items():
                if row.get(field) != val:
                    row[field] = val
            changed += 1

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    writer.writerows(rows)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(output.getvalue())

    print(f"Updated {changed} rows")

if __name__ == "__main__":
    apply_updates()
