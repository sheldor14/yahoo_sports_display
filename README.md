# Yahoo Fantasy Baseball Rankings

A simple webapp that shows how every team in your Yahoo Fantasy Baseball league ranks across each head-to-head stat category for a given week.

## What it does

Enter your league ID and a week number, and the app fetches that week's scoreboard from the Yahoo Fantasy Sports API. It then ranks every team from 1 (worst) to N (best, where N is the number of teams) for each stat. Stats where lower is better (ERA, WHIP) are ranked in reverse. Tied teams share the average of the positions they occupy — for example, two teams tied for spots 10 and 11 each score 10.5. The final table is sorted by total score so you can see who dominated the week across all categories.

## Setup

**1. Install dependencies**
```
pip install -r requirements.txt
```

**2. Create a Yahoo Developer App**
- Go to [developer.yahoo.com/apps](https://developer.yahoo.com/apps/) and create a new app
- Set the **Redirect URI** to `http://localhost:5000/auth/callback`
- Enable the **Fantasy Sports** API permission (Read)

**3. Configure your credentials**
```
cp .env.example .env
```
Fill in `YAHOO_CLIENT_ID` and `YAHOO_CLIENT_SECRET` from your Yahoo app.

**4. Run the server**
```
python app.py
```

**5. Authenticate**

Open `http://localhost:5000`, follow the setup banner to connect your Yahoo account. The OAuth tokens are saved back to `.env` automatically and refresh silently when they expire.

## Usage

Enter your **League ID** (the number in your Yahoo league URL) and the **week number**, then click **Get Rankings**.
