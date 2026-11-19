# PHOENIX
### AI-Powered Global Urban Disaster Intelligence Platform
*Turning Disaster Data Into Actionable Intelligence* · BAU Innovate 2026 prototype

PHOENIX helps disaster managers see **what infrastructure a flood scenario would reach**, not just where the
water goes. One shared analysis engine serves several cities; each city only supplies data files.

| City | Flood hazard data | Infrastructure | Extra |
|---|---|---|---|
| **Freetown**, Sierra Leone | World Bank / JBA modelled extents, 1-in-20 & 1-in-100 yr | OSM facilities + major roads | 153k modelled buildings, ward losses, City Council flood records |
| **New Orleans**, LA | FEMA NFHL (1% and 0.2% annual chance; levee zone shown) | OSM facilities + major roads | NWS alerts, Mississippi GloFAS |
| **Houston**, TX | FEMA NFHL, central study area | OSM facilities + major roads | NWS alerts, GloFAS |
| **Miami**, FL | FEMA NFHL (incl. VE coastal zones) | OSM facilities + major roads | NWS alerts |

## 1. Run it

```powershell
cd PHOENIX_Hackathon_Starter
.venv\Scripts\Activate.ps1          # or: python -m venv .venv  first
pip install -r requirements.txt
streamlit run app.py
```

Optional: set `ANTHROPIC_API_KEY` to let the AI assistant use Claude for free-form questions
(`$env:ANTHROPIC_API_KEY="sk-ant-..."` in PowerShell). Without it, a built-in grounded analyst answers.

Tests: `python -m pytest -q`

## 2. Sections

1. **Overview** - KPIs, exposed-facility map, auto-generated key findings, data status and limitations.
2. **Live Intelligence** - Open-Meteo rainfall forecast, Copernicus GloFAS river discharge, US NWS alerts.
   Labelled as FORECAST/ALERT data and never mixed with scenario layers. Needs internet.
3. **Flood Risk Analysis** - all scenarios as switchable layers, hazard area by scenario, FEMA zone breakdown,
   Freetown wards, building density and "model vs recorded flooding" gap.
4. **Infrastructure Exposure** - every facility by type (exposed ones ringed red), flooded road sections,
   bridges, downloadable list of exposed facilities.
5. **WHAT IF? Scenario Simulator** - compare scenarios; hazard-edge sensitivity (0-500 m); service disruption
   (if exposed hospitals / fire stations / police go offline, how much further is the nearest working one?).
   The old rainfall slider is kept as a clearly labelled *illustrative* teaching scenario.
6. **AI Intelligence Assistant** - answers only from a fact sheet built from the analysis on screen (shown
   for audit). Uses Claude if an API key is set.
7. **Flood Alerts & SMS Notifications** - opt-in SMS alerts for registered people (see section 7 below).
8. **Data Sources and Methodology** - dataset status, dates, licences, limitations, missing data, method.

Sidebar: city, section, flood scenario, screening distance, basemap, and **Bring your own data**
(an uploaded hazard GeoJSON becomes a selectable scenario; an uploaded asset CSV is analysed with the rest).

## 3. Project layout

```
app.py                     Streamlit UI (all 7 sections)
phoenix_core/
  cities.py                City registry: scenarios, datasets, missing data, caveats
  analysis.py              Exposure engine: facilities, roads, hazard area, service-gap what-ifs
  geo.py                   Local metric projection, CRS checks, geometry repair
  data.py                  Loaders
  maps.py                  Folium layers, markers, legend (keyless basemaps only)
  live.py                  Open-Meteo, GloFAS, NWS (no keys)
  assistant.py             Grounded analyst + optional Claude
  theme.py                 Dark intelligence-console styling
scripts/
  fetch_city_data.py       Download FEMA / TIGERweb / OSM raw data
  build_city_data.py       Raw data -> data/cities/<city>/ (clip, dissolve, categorise, manifest)
prepare_data.py            Freetown World Bank processing (unchanged)
data/processed/            Freetown World Bank outputs (unchanged)
data/cities/<city>/        boundary, fema_*.geojson, facilities, roads, manifest.json
data/raw/                  Raw downloads used to build data/cities (for reproducibility)
tests/test_core.py         Unit + data-integrity tests
tests/test_alerts*.py      Alerts & SMS tests (faked Twilio/feeds, no network, no messages)
phoenix_core/alert*.py, recipient_manager.py, sms_service.py   Flood Alerts & SMS feature
scripts/run_alert_monitor.py                                    Stand-alone alert monitor
data/private/              Local alerts database (git-ignored; created on first use)
backups/                   Code backups taken before major changes
app_backup_original.py     Original starter (kept)
```

## 4. Refreshing / adding data

```powershell
python scripts/fetch_city_data.py --out data/raw
python scripts/build_city_data.py --raw data/raw
```
`build_city_data.py` also accepts OSM extracts from the HOT OSM Raw Data API
(`phoenix_hot_<facilities|roads>_<city>.zip`), which is what this release used because the public Overpass
servers were overloaded on 19 Nov 2026. To add a city: add it to `CITIES` in both scripts and to
`phoenix_core/cities.py`. No UI changes are needed.

## 5. Honesty rules built into the app

- Hazard layers are **scenarios** (probabilistic or regulatory), never presented as forecasts.
- *Exposed* = located inside (or within the chosen distance of) a hazard area. It is not a damage estimate.
- No names or coordinates are invented: unnamed OSM features are shown as "Unnamed ... (OSM id)".
- Cities or datasets that are missing are shown as missing; no analysis is produced without hazard data.
- Uploaded layers not in longitude/latitude (EPSG:4326) are rejected instead of being drawn in the wrong place.

## 6. Known limitations

- Freetown facility checks use the simplified display extent (~4 m, fragments < 200 m2 dropped), so they can
  miss assets in very small surface-water pockets; building counts use the full-resolution overlay.
  The screening-distance slider helps cover this.
- FEMA zones: regulatory 1% / 0.2% floods; little pluvial (street) flooding; New Orleans' levee-protected
  Zone X is shown for context but is not part of either scenario.
- OSM completeness differs between cities; straight-line distances only; no population data loaded.
- Houston is analysed for a central study area (95.50-95.25 W, 29.68-29.84 N), not the whole city.

## 7. Flood Alerts & SMS Notifications

**What it does.** An administrator registers people who have explicitly agreed to receive flood messages (name or
nickname, international mobile number, city, ward / neighbourhood and an approximate location, preferred alert
level). PHOENIX checks credible sources for each registered location and produces explained, categorised alerts:

| Source | Cities | Can produce | Message says |
|---|---|---|---|
| US National Weather Service active alerts (queried at each location) | New Orleans, Houston, Miami | INFORMATIONAL to CRITICAL (Flood Advisory → Watch → Warning → Flash Flood Emergency / Storm Surge Warning) | "US National Weather Service *Flood Warning* in effect for your registered area until ..." |
| Open-Meteo rainfall forecast vs an administrator-set screening threshold + whether the location is in a mapped flood zone | all four | INFORMATIONAL, or ELEVATED if in a mapped zone (never higher) | "PHOENIX flood risk notice ... Not an official warning" |
| Flood hazard maps (World Bank / FEMA) | all four | nothing on their own; context only | - |

**Message content (max 459 characters = 3 SMS segments).** Each SMS gives the city and the person's own registered
neighbourhood, the risk level, a one-line reason, 3-4 preventive actions chosen for that level (e.g. never walk or
drive through floodwater; move documents, valuables and electricals up high; ready water, medicine, a flashlight and
supplies; keep the phone charged), and a reminder to follow official alerts and local authorities. PHOENIX forecast
notices say "Not an official warning" and "Flooding possible, not certain", and never advise evacuation. "Move to
higher ground if authorities instruct" is added only for HIGH/CRITICAL, which only an official NWS warning can
produce; those messages also say "PHOENIX is not an emergency service". If space runs short, lower-priority advice
is dropped first (never below 3), then the reason line is shortened; labels, reminder and STOP/HELP are never cut.
The *Send Test SMS* tab has a "Demonstration: flood risk notice + preventive advice" message that shows this format
and is clearly marked as a demonstration example.

Freetown has no machine-readable official warning feed integrated, so it can only receive PHOENIX forecast notices.
The rainfall threshold is a local setting, **not** a validated flood trigger; agree it with local experts.

**Access.** Disaster intelligence (all other sections) is public. In this section the public sees only current alerts
(official warning areas; forecast notices by city only) and a system-status table. Recipient management and all
SMS operations require administrator sign-in with `PHOENIX_ADMIN_PASSCODE`; **if it is not set, the admin tools are
disabled**. Sessions expire after 60 minutes of inactivity; 5 wrong passcodes lock sign-in for 15 minutes.

**Safety rules (enforced in code and tested):** no registration without recorded consent · nothing sent to people
who opted out, disabled SMS, or chose a higher minimum level · the record is re-read just before each send so an
opt-out applies immediately · never the same event twice (NWS update chains are followed) · per-person cooldown that
only a genuine escalation can break · daily cap · every message names PHOENIX, says it is not a government message
where relevant, and ends with "Reply STOP to opt out, HELP for help" · test messages start with
"TEST ONLY - NOT A REAL ALERT" and go to one person only · nothing is sent when a page opens or a control changes.

### Administrator setup

Technical setup is kept out of the normal interface. After signing in, administrators find credential setup,
backup instructions and troubleshooting in the **Setup & help** tab. The normal screens show only simple status:
SMS *Connected / Not configured / Connection error*, a masked sender number, monitoring and auto-send status.
Provider error text is redacted (phone numbers, Twilio IDs and tokens removed) before it is stored or shown.
Credentials are read from the server environment only; `.gitignore` excludes `.env`, `.streamlit/secrets.toml`,
the local database folder and backups so none of them can be committed.

**SMS delivery (Twilio).** There is no simulation mode in the application. Sending is **disabled** until the
Twilio variables are set in the terminal before `streamlit run app.py` (never in files):

```powershell
$env:PHOENIX_ADMIN_PASSCODE="choose-a-strong-passcode"
$env:TWILIO_ACCOUNT_SID="AC..."; $env:TWILIO_AUTH_TOKEN="..."; $env:TWILIO_PHONE_NUMBER="+1..."
# optional: $env:TWILIO_MESSAGING_SERVICE_SID="MG..."   (send through a Messaging Service, e.g. for US A2P 10DLC)
```
Then *Notification settings > Check connection* checks the account and sender number with Twilio without sending
anything. Group sends need ticking a confirmation and typing SEND; test sends go to one person and need TEST.
A message counts as *Delivered (confirmed)* only after Twilio reports it (*Refresh delivery status*, also done on
each monitor cycle). Twilio rules: US numbers need A2P 10DLC registration (or a verified toll-free number); check
availability and sender-ID rules for Sierra Leone (+232); a trial account only delivers to verified numbers.

**What is operational.** The *System status* table reports each capability from evidence recorded on this
installation (e.g. "Operational - delivery confirmed by Twilio <time>") and otherwise names the missing dependency.
Nothing is shown as operational before it has worked here. The fake provider in `sms_service.MockProvider` is used
**only** by the automated tests and cannot be selected in the app.

**Monitoring.** *Check sources now* generates alerts for review and never sends. *Start app monitor* checks every N
minutes **only while the Streamlit app is running**. For monitoring independent of the browser, run the worker:
```powershell
python scripts/run_alert_monitor.py            # loop (Ctrl+C to stop); --once for a single check
```
(Windows Task Scheduler can start it at log-on.) The dashboard shows a monitor as *Running* only while it is sending
heartbeats. Automatic sending is off by default and additionally requires Twilio to be configured and
`PHOENIX_ALLOW_AUTOMATED_SMS=1` (a deliberate switch for unattended real SMS).

**Replies.** Twilio applies STOP/HELP automatically for US numbers; if Twilio reports a recipient has replied STOP
(error 21610) PHOENIX marks them opted out. Other replies can be recorded in *Registered recipients*
(`recipient_manager.handle_inbound` is ready to be connected to a provider webhook in a hosted deployment).

**Data storage.** `data/private/phoenix_alerts.db` (SQLite; override with `PHOENIX_ALERTS_DB`). The folder has a
`.gitignore` so phone numbers are never committed. Back up by copying the file while the app and worker are stopped,
and keep backups as securely as the original. Phone numbers are shown masked everywhere in the app; deleting a
person removes their record (delivery logs keep only the masked number).

**Modules:** `phoenix_core/alert_store.py` (database), `recipient_manager.py` (validation, consent, opt-out),
`sms_service.py` (Twilio over HTTPS, no extra packages; test-only fake), `alerts.py` (engine, messages, delivery rules),
`alert_monitor.py` (checks, app monitor, heartbeats), `alerts_ui.py` (screen), `scripts/run_alert_monitor.py`
(worker). Tests: `tests/test_alerts.py` and `tests/test_alerts_ui.py` (Twilio HTTP and data feeds faked; no
network, no messages).

## 8. Attribution

World Bank *Freetown, Sierra Leone Flood Risk Assessment* (JBA Risk Management, GEM Foundation), CC BY 4.0 ·
FEMA National Flood Hazard Layer (via Esri Living Atlas *USA Flood Hazard Areas*), public domain ·
US Census Bureau TIGERweb, public domain · © OpenStreetMap contributors, ODbL 1.0 (extracts via HOT OSM Raw Data
API and Overpass) · Open-Meteo, CC BY 4.0 · Copernicus EMS GloFAS · US National Weather Service ·
Basemaps © OpenStreetMap contributors, Esri.
