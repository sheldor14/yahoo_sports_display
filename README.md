# Yahoo Fantasy Baseball Rankings

A simple webapp that shows how every team in your Yahoo Fantasy Baseball league ranks across each head-to-head stat category, for a single week or across a range of weeks.

## What it does

Enter your league ID and a week number, and the app fetches that week's scoreboard from the Yahoo Fantasy Sports API. It then ranks every team from 1 (worst) to N (best, where N is the number of teams) for each stat. Stats where lower is better (ERA, WHIP) are ranked in reverse. Tied teams share the average of the positions they occupy — for example, two teams tied for spots 10 and 11 each score 10.5. The final table is sorted by total score so you can see who dominated the week across all categories.

Only scoring categories are ranked; display-only stats such as IP and H/AB are left out.

Each team also gets an **All-Play** record: the win-loss-tie record it would have had that week if it had played every other team, deciding each pairing category by category. It shows who was strong that week, whatever their actual opponent was.

The **Season** view adds up each team's weekly totals across a range of weeks, shows where each team placed each week, and combines their all-play records.

## Setup

**1. Install dependencies**

Use a virtual environment so the app's packages don't clash with the ones your system Python ships with:
```
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Create a Yahoo Developer App**
- Go to [developer.yahoo.com/apps](https://developer.yahoo.com/apps/) and create a new app
- Set the **Redirect URI** to `https://localhost:5000/auth/callback` (it must be `https`)
- Enable the **Fantasy Sports** API permission (Read)

**3. Configure your credentials**
```
cp config.example.txt config.txt
```
Fill in `client_id` and `client_secret` under the `[yahoo]` section.

**4. Run the server**
```
python app.py
```

**5. Authenticate**

Open `https://localhost:5000` (your browser will warn about the self-signed certificate — that's expected locally), follow the setup banner to connect your Yahoo account. The OAuth tokens are saved back to `config.txt` automatically and refresh silently when they expire. Only the token lines are rewritten, so your comments in `config.txt` are kept.

## Usage

Enter your **League ID** (the number in your Yahoo league URL). The app looks up the league and fills in the current week; it also remembers the league ID for next time. Then choose a view and click **Get Rankings**:

- **Single week**: every stat ranked for one week, plus each team's all-play record.
- **Season**: each team's weekly totals from one week to another, with the season total, average and combined all-play record.

To look at a past year, enter it under **Season** (leave it blank for the current season). Click any column heading to sort by it.

## Tests

The ranking math, all-play and season logic, Yahoo response parsing, config writing, and the API routes (against canned Yahoo responses) are covered by unit tests using the standard-library `unittest`:

```
python -m unittest
```
